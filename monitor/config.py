import os
from dataclasses import dataclass, field
from typing import Dict, List

@dataclass
class WANConfig:
    id: str
    name: str
    target_ip: str
    gateway_ip: str
    interface_label: str

@dataclass
class MonitorConfig:
    port: int = int(os.getenv("METRICS_PORT", "8000"))
    ping_interval_seconds: int = int(os.getenv("PING_INTERVAL", "10"))
    ping_count_per_cycle: int = int(os.getenv("PING_COUNT", "5"))
    speedtest_interval_seconds: int = int(os.getenv("SPEEDTEST_INTERVAL", "1800"))
    
    # Histerese e Proteção contra Flapping
    flapping_min_score_diff: float = float(os.getenv("FLAPPING_MIN_SCORE_DIFF", "5.0"))
    flapping_confirmation_cycles: int = int(os.getenv("FLAPPING_CONFIRM_CYCLES", "3"))

    # Configurações do Roteador ER605 e Failover Automático
    er605_host: str = os.getenv("ER605_HOST", "192.168.10.1")
    er605_user: str = os.getenv("ER605_USER", "myhouse")
    er605_pass: str = os.getenv("ER605_PASS", "24865310@Asd")
    failover_enabled: bool = os.getenv("FAILOVER_ENABLED", "true").lower() == "true"
    failover_interval_seconds: int = int(os.getenv("FAILOVER_INTERVAL", "3600"))  # Avaliação a cada 1 hora
    emergency_failover_enabled: bool = os.getenv("EMERGENCY_FAILOVER", "true").lower() == "true"

    wans: Dict[str, WANConfig] = field(default_factory=lambda: {
        "wan1": WANConfig(
            id="wan1",
            name=os.getenv("WAN1_NAME", "Operadora 1"),
            target_ip=os.getenv("WAN1_TARGET", "8.8.8.8"),
            gateway_ip="192.168.0.1",
            interface_label="WAN"
        ),
        "wan2": WANConfig(
            id="wan2",
            name=os.getenv("WAN2_NAME", "Operadora 2"),
            target_ip=os.getenv("WAN2_TARGET", "8.8.4.4"),
            gateway_ip="192.168.1.254",
            interface_label="WAN/LAN1"
        )
    })

config = MonitorConfig()
