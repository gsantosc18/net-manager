import collections
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from ping_collector import PingResult

@dataclass
class QualityMetrics:
    wan_id: str
    wan_name: str
    is_available: bool
    latency_ms: float
    jitter_ms: float
    packet_loss_pct: float
    download_mbps: Optional[float]
    upload_mbps: Optional[float]
    instant_score: float
    smoothed_score: float
    status_label: str  # "EXCELLENT", "GOOD", "DEGRADED", "OFFLINE"

class HealthScorer:
    def __init__(self, history_size: int = 12, min_score_diff: float = 5.0, confirm_cycles: int = 3):
        self.history_size = history_size
        self.min_score_diff = min_score_diff
        self.confirm_cycles = confirm_cycles
        
        # Histórico recente de scores instantâneos por WAN
        self.history: Dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=self.history_size)
        )
        
        # Estado de decisão e proteção contra flapping
        self.current_recommended_primary: Optional[str] = None
        self.candidate_primary: Optional[str] = None
        self.candidate_cycles_count: int = 0
        self.last_switch_timestamp: float = 0.0
        self.cooldown_seconds: float = 60.0  # Mínimo 1 minuto entre recomendações de mudança

    def calculate_instant_score(self, ping: PingResult, download_mbps: Optional[float] = None) -> float:
        """
        Calcula a pontuação de qualidade instantânea de 0.0 a 100.0.
        Fórmula ponderada:
        - Disponibilidade: 100% ou 0%
        - Perda de Pacotes (peso 40%): penalidade exponencial
        - Latência (peso 35%): pontuação proporcional (0 a 100)
        - Jitter (peso 25%): pontuação proporcional (0 a 100)
        """
        if not ping.is_available or ping.packet_loss_pct >= 95.0:
            return 0.0

        # 1. Componente de Perda de Pacotes (0 a 100)
        # 0% perda = 100 pts; 1% perda = 80 pts; 5% perda = 20 pts; >=10% = 0 pts
        loss = ping.packet_loss_pct
        if loss == 0.0:
            score_loss = 100.0
        elif loss <= 2.0:
            score_loss = max(0.0, 100.0 - (loss * 15.0))
        elif loss <= 10.0:
            score_loss = max(0.0, 70.0 - ((loss - 2.0) * 8.0))
        else:
            score_loss = 0.0

        # 2. Componente de Latência (0 a 100)
        # <= 15ms = 100 pts; 50ms = 70 pts; 100ms = 35 pts; >= 180ms = 0 pts
        lat = ping.latency_avg_ms
        if lat <= 15.0:
            score_latency = 100.0
        elif lat <= 60.0:
            score_latency = max(0.0, 100.0 - ((lat - 15.0) * 0.7))
        elif lat <= 150.0:
            score_latency = max(0.0, 68.5 - ((lat - 60.0) * 0.6))
        else:
            score_latency = max(0.0, 15.0 - ((lat - 150.0) * 0.5))

        # 3. Componente de Jitter (0 a 100)
        # <= 3ms = 100 pts; 10ms = 70 pts; 25ms = 25 pts; >= 40ms = 0 pts
        jit = ping.jitter_ms
        if jit <= 3.0:
            score_jitter = 100.0
        elif jit <= 12.0:
            score_jitter = max(0.0, 100.0 - ((jit - 3.0) * 3.5))
        elif jit <= 30.0:
            score_jitter = max(0.0, 68.5 - ((jit - 12.0) * 3.0))
        else:
            score_jitter = 0.0

        instant_score = (score_loss * 0.40) + (score_latency * 0.35) + (score_jitter * 0.25)
        return round(max(0.0, min(100.0, instant_score)), 2)

    def evaluate_wan(self, wan_id: str, wan_name: str, ping: PingResult, download_mbps: Optional[float] = None, upload_mbps: Optional[float] = None) -> QualityMetrics:
        instant = self.calculate_instant_score(ping, download_mbps)
        self.history[wan_id].append(instant)
        
        # Média móvel ponderada (valores mais recentes têm maior peso)
        history_list = list(self.history[wan_id])
        if len(history_list) == 1:
            smoothed = history_list[0]
        else:
            weights = list(range(1, len(history_list) + 1))
            smoothed = sum(s * w for s, w in zip(history_list, weights)) / sum(weights)
        smoothed = round(smoothed, 2)

        # Determina o rótulo de status
        if not ping.is_available:
            label = "OFFLINE"
        elif smoothed >= 85.0:
            label = "EXCELLENT"
        elif smoothed >= 65.0:
            label = "GOOD"
        else:
            label = "DEGRADED"

        return QualityMetrics(
            wan_id=wan_id,
            wan_name=wan_name,
            is_available=ping.is_available,
            latency_ms=ping.latency_avg_ms,
            jitter_ms=ping.jitter_ms,
            packet_loss_pct=ping.packet_loss_pct,
            download_mbps=download_mbps,
            upload_mbps=upload_mbps,
            instant_score=instant,
            smoothed_score=smoothed,
            status_label=label
        )

    def decide_recommended_primary(self, metrics: Dict[str, QualityMetrics]) -> tuple[str, bool]:
        """
        Aplica algoritmo de histerese e proteção contra flapping.
        Retorna: (wan_id_recomendada, houve_mudanca_confirmada)
        """
        now = time.time()
        
        # Ordena as WANs pelo score suavizado decrescente
        sorted_wans = sorted(metrics.values(), key=lambda m: m.smoothed_score, reverse=True)
        if not sorted_wans:
            return "none", False
            
        leader = sorted_wans[0]
        runner_up = sorted_wans[1] if len(sorted_wans) > 1 else None

        # Inicialização no primeiro ciclo
        if self.current_recommended_primary is None:
            self.current_recommended_primary = leader.wan_id
            self.last_switch_timestamp = now
            return leader.wan_id, True

        current_primary_id = self.current_recommended_primary
        current_primary_metrics = metrics.get(current_primary_id)

        # Regra 1: Falha Total da Primária Atual (Prioridade Máxima)
        if current_primary_metrics and not current_primary_metrics.is_available:
            if leader.is_available and leader.wan_id != current_primary_id:
                self.current_recommended_primary = leader.wan_id
                self.candidate_primary = None
                self.candidate_cycles_count = 0
                self.last_switch_timestamp = now
                return leader.wan_id, True

        # Regra 2: Histerese de Desempenho
        # O líder só desafia a primária atual se seu score for superior por pelo menos min_score_diff
        current_score = current_primary_metrics.smoothed_score if current_primary_metrics else 0.0
        score_advantage = leader.smoothed_score - current_score

        if leader.wan_id != current_primary_id and score_advantage >= self.min_score_diff:
            # Proteção de Cooldown
            if now - self.last_switch_timestamp >= self.cooldown_seconds:
                if self.candidate_primary == leader.wan_id:
                    self.candidate_cycles_count += 1
                else:
                    self.candidate_primary = leader.wan_id
                    self.candidate_cycles_count = 1

                # Confirmação de persistência por N ciclos consecutivos
                if self.candidate_cycles_count >= self.confirm_cycles:
                    self.current_recommended_primary = leader.wan_id
                    self.candidate_primary = None
                    self.candidate_cycles_count = 0
                    self.last_switch_timestamp = now
                    return leader.wan_id, True
        else:
            # Líder não tem vantagem suficiente ou primária atual recuperou a liderança
            self.candidate_primary = None
            self.candidate_cycles_count = 0

        return self.current_recommended_primary, False
