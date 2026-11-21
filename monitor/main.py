import logging
import time
import threading
import schedule
from prometheus_client import start_http_server, Gauge, Counter
from config import config
from ping_collector import measure_ping
from scorer import HealthScorer
from speedtest_collector import SpeedtestManager

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
GAUGE_RECOMMENDED_PRIMARY = Gauge("isp_recommended_primary", "Indica se a WAN é a primária recomendada (1=Sim, 0=Não)", ["wan", "name"])
COUNTER_SWITCH_RECOMMENDATIONS = Counter("isp_switch_recommendations_total", "Total de vezes que o algoritmo recomendou troca de WAN", ["from_wan", "to_wan"])

scorer = HealthScorer(
    history_size=12,
    min_score_diff=config.flapping_min_score_diff,
    confirm_cycles=config.flapping_confirmation_cycles
)
speedtest_mgr = SpeedtestManager()

def run_ping_cycle():
    """Executado a cada 10 segundos"""
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
            (f" | Down: {down}M Up: {up}M" if down else "")
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
            f"⚡ [DECISÃO DE FAILOVER] Melhor conexão alterada de {previous_primary} para {recommended_primary}!"
        )

def run_speedtest_cycle():
    """Executa o teste de velocidade para ambas as operadoras"""
    speedtest_mgr.run_all_async()

def delayed_initial_speedtest():
    time.sleep(5)
    logger.info("Disparando medição inicial de velocidade para ambas as conexões...")
    speedtest_mgr.run_all_async()

def main():
    logger.info(f"Iniciando servidor de métricas Prometheus na porta {config.port}...")
    start_http_server(config.port)
    
    run_ping_cycle()
    
    # Inicia teste de velocidade inicial em background
    threading.Thread(target=delayed_initial_speedtest, daemon=True).start()
    
    schedule.every(config.ping_interval_seconds).seconds.do(run_ping_cycle)
    schedule.every(config.speedtest_interval_seconds).seconds.do(run_speedtest_cycle)
    
    logger.info(f"Monitoramento ativo. Ciclos de teste a cada {config.ping_interval_seconds} segundos.")
    
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
