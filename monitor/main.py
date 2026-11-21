import logging
import time
import threading
import schedule
from prometheus_client import start_http_server, Gauge, Counter
from config import config
from ping_collector import measure_ping
from scorer import HealthScorer
from speedtest_collector import SpeedtestManager

from er605_controller import ER605Controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("net_monitor")

# --- Métricas Prometheus ---
GAUGE_AVAILABLE = Gauge("isp_probe_available", "Disponibilidade da conexão (1=Up, 0=Down)", ["wan", "name"])
GAUGE_LATENCY = Gauge("isp_probe_latency_ms", "Tempo de resposta em ms", ["wan", "name"])
GAUGE_JITTER = Gauge("isp_probe_jitter_ms", "Oscilação da conexão em ms", ["wan", "name"])
GAUGE_PACKET_LOSS = Gauge("isp_probe_packet_loss_percent", "Percentual de falhas e perda", ["wan", "name"])
GAUGE_HEALTH_SCORE = Gauge("isp_health_score", "Nota geral de qualidade da conexão (0-100)", ["wan", "name"])
GAUGE_DOWNLOAD = Gauge("isp_download_mbps", "Velocidade de download em Mbps", ["wan", "name"])
GAUGE_UPLOAD = Gauge("isp_upload_mbps", "Velocidade de upload em Mbps", ["wan", "name"])
GAUGE_RECOMMENDED_PRIMARY = Gauge("isp_recommended_primary", "Indica se a WAN é a primária recomendada pelo algoritmo (1=Sim, 0=Não)", ["wan", "name"])
GAUGE_ACTIVE_ROUTER_PRIMARY = Gauge("isp_active_router_primary", "Indica qual WAN está ativa como principal no roteador ER605 (1=Sim, 0=Não)", ["wan", "name"])
COUNTER_SWITCH_RECOMMENDATIONS = Counter("isp_switch_recommendations_total", "Total de vezes que o algoritmo recomendou troca de WAN", ["from_wan", "to_wan"])
COUNTER_FAILOVER_EXECUTED = Counter("isp_failover_executed_total", "Total de trocas de rota executadas no ER605", ["from_wan", "to_wan", "reason"])
GAUGE_NEXT_HOURLY_EVAL_SECONDS = Gauge("isp_next_hourly_eval_seconds", "Segundos restantes para a próxima avaliação horária de failover")

scorer = HealthScorer(
    history_size=12,
    min_score_diff=config.flapping_min_score_diff,
    confirm_cycles=config.flapping_confirmation_cycles
)
speedtest_mgr = SpeedtestManager()
er605 = ER605Controller(
    host=config.er605_host,
    username=config.er605_user,
    password=config.er605_pass
)

# Estado do Failover
current_active_router_wan: str = "wan2"
last_hourly_eval_time: float = time.time()
active_wan_consecutive_failures: int = 0

def run_ping_cycle():
    """Executado a cada 10 segundos"""
    global current_active_router_wan, active_wan_consecutive_failures
    wan_evaluations = {}
    
    for wan_id, wan in config.wans.items():
        ping_res = measure_ping(wan.target_ip, count=config.ping_count_per_cycle)
        st_res = speedtest_mgr.get_result(wan_id)
        
        down = st_res.download_mbps if st_res else None
        up = st_res.upload_mbps if st_res else None
        
        eval_res = scorer.evaluate_wan(wan_id, wan.name, ping_res, down, up)
        wan_evaluations[wan_id] = eval_res
        
        labels = [wan_id, wan.name]
        GAUGE_AVAILABLE.labels(*labels).set(1 if ping_res.is_available else 0)
        GAUGE_LATENCY.labels(*labels).set(ping_res.latency_avg_ms)
        GAUGE_JITTER.labels(*labels).set(ping_res.jitter_ms)
        GAUGE_PACKET_LOSS.labels(*labels).set(ping_res.packet_loss_pct)
        GAUGE_HEALTH_SCORE.labels(*labels).set(eval_res.smoothed_score)
        
        # Métrica indicando se esta WAN é a principal no hardware do ER605
        is_active = 1 if wan_id == current_active_router_wan else 0
        GAUGE_ACTIVE_ROUTER_PRIMARY.labels(*labels).set(is_active)
        
        if down is not None:
            GAUGE_DOWNLOAD.labels(*labels).set(down)
        if up is not None:
            GAUGE_UPLOAD.labels(*labels).set(up)
            
        logger.info(
            f"[{wan.name} ({wan_id.upper()})] "
            f"Nota: {eval_res.smoothed_score}/100 | "
            f"Resposta: {ping_res.latency_avg_ms}ms | "
            f"Oscilação: {ping_res.jitter_ms}ms | "
            f"Falhas: {ping_res.packet_loss_pct}%" +
            (f" | Down: {down}M Up: {up}M" if down else "") +
            (" [ROTA ATIVA NO ER605]" if is_active else "")
        )

    # Avaliação de decisão e histerese
    previous_primary = scorer.current_recommended_primary
    recommended_primary, changed = scorer.decide_recommended_primary(wan_evaluations)
    
    for wan_id, wan in config.wans.items():
        is_rec = 1 if wan_id == recommended_primary else 0
        GAUGE_RECOMMENDED_PRIMARY.labels(wan_id, wan.name).set(is_rec)
        
    if changed and previous_primary and previous_primary != recommended_primary:
        COUNTER_SWITCH_RECOMMENDATIONS.labels(from_wan=previous_primary, to_wan=recommended_primary).inc()
        logger.warning(
            f"⚡ [DECISÃO DE QUALIDADE] Melhor conexão alterada de {previous_primary} para {recommended_primary}!"
        )

    # Atualiza contador de tempo para a próxima avaliação de 1 hora
    remaining_secs = max(0, config.failover_interval_seconds - int(time.time() - last_hourly_eval_time))
    GAUGE_NEXT_HOURLY_EVAL_SECONDS.set(remaining_secs)

    # Proteção de Emergência: se a WAN principal atual cair completamente, comutar imediatamente
    if config.emergency_failover_enabled and len(wan_evaluations) >= 2:
        active_eval = wan_evaluations.get(current_active_router_wan)
        alt_wan_id = "wan1" if current_active_router_wan == "wan2" else "wan2"
        alt_eval = wan_evaluations.get(alt_wan_id)

        if active_eval and alt_eval:
            if not active_eval.is_available or active_eval.packet_loss_pct >= 80.0:
                active_wan_consecutive_failures += 1
                logger.warning(
                    f"⚠️ Conexão ativa no ER605 ({current_active_router_wan.upper()}) com instabilidade severa! "
                    f"Ciclo {active_wan_consecutive_failures}/3"
                )
                if active_wan_consecutive_failures >= 3 and alt_eval.is_available and alt_eval.packet_loss_pct < 20.0:
                    logger.critical(
                        f"🚨 [FAILOVER DE EMERGÊNCIA] {current_active_router_wan.upper()} fora do ar! "
                        f"Comutando imediatamente para {alt_wan_id.upper()}..."
                    )
                    if er605.set_primary_wan(alt_wan_id):
                        COUNTER_FAILOVER_EXECUTED.labels(
                            from_wan=current_active_router_wan,
                            to_wan=alt_wan_id,
                            reason="emergency_drop"
                        ).inc()
                        current_active_router_wan = alt_wan_id
                        active_wan_consecutive_failures = 0
            else:
                active_wan_consecutive_failures = 0

def run_hourly_failover_job():
    """Executado a cada 1 hora para comutar o tráfego para a melhor operadora"""
    global last_hourly_eval_time, current_active_router_wan
    last_hourly_eval_time = time.time()
    
    if not config.failover_enabled:
        logger.info("[FAILOVER 1H] Failover desativado por configuração. Apenas monitorando.")
        return
        
    recommended = scorer.current_recommended_primary
    if not recommended:
        logger.warning("[FAILOVER 1H] Sem dados suficientes para decisão horária.")
        return
        
    logger.info(
        f"⏰ [AVALIAÇÃO HORÁRIA] Analisando tráfego... "
        f"Ativa no Roteador: {current_active_router_wan.upper()} | Melhor Operadora: {recommended.upper()}"
    )
    
    if recommended != current_active_router_wan:
        logger.warning(
            f"⚡ [FAILOVER 1H] A {recommended.upper()} está com nota superior. "
            f"Comutando rota principal no ER605 de {current_active_router_wan.upper()} para {recommended.upper()}..."
        )
        if er605.set_primary_wan(recommended):
            COUNTER_FAILOVER_EXECUTED.labels(
                from_wan=current_active_router_wan,
                to_wan=recommended,
                reason="hourly_best_score"
            ).inc()
            current_active_router_wan = recommended
            logger.info(f"✅ [FAILOVER 1H] Conexão comutada com sucesso para {recommended.upper()}!")
        else:
            logger.error(f"❌ [FAILOVER 1H] Falha ao comutar no ER605.")
    else:
        logger.info(
            f"✨ [FAILOVER 1H] A conexão {current_active_router_wan.upper()} continua sendo a melhor. Mantendo sem interrupções."
        )

def run_speedtest_cycle():
    """Executa o teste de velocidade para ambas as operadoras"""
    speedtest_mgr.run_all_async()

def delayed_initial_speedtest():
    time.sleep(5)
    logger.info("Disparando medição inicial de velocidade para ambas as conexões...")
    speedtest_mgr.run_all_async()

def init_router_state():
    global current_active_router_wan
    try:
        er605.ensure_online_detection_always_on()
        active = er605.get_active_primary_wan()
        if active:
            current_active_router_wan = active
            logger.info(f"Estado inicial do ER605: rota principal ativa é {current_active_router_wan.upper()}")
    except Exception as ex:
        logger.warning(f"Não foi possível obter estado inicial do ER605: {ex}")

def main():
    logger.info(f"Iniciando servidor de métricas Prometheus na porta {config.port}...")
    start_http_server(config.port)
    
    init_router_state()
    run_ping_cycle()
    
    # Inicia teste de velocidade inicial em background
    threading.Thread(target=delayed_initial_speedtest, daemon=True).start()
    
    schedule.every(config.ping_interval_seconds).seconds.do(run_ping_cycle)
    schedule.every(config.speedtest_interval_seconds).seconds.do(run_speedtest_cycle)
    schedule.every(config.failover_interval_seconds).seconds.do(run_hourly_failover_job)
    
    logger.info(
        f"Monitoramento e Failover Ativos:\n"
        f" - Testes de ping/qualidade: a cada {config.ping_interval_seconds}s\n"
        f" - Testes de velocidade: a cada {config.speedtest_interval_seconds}s\n"
        f" - Avaliação e chaveamento de rota: a cada {config.failover_interval_seconds}s (1 hora)"
    )
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
