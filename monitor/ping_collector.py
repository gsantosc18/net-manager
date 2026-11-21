import re
import subprocess
import time
import statistics
from dataclasses import dataclass
from typing import Optional

try:
    from icmplib import ping as icmp_ping
    HAS_ICMPLIB = True
except ImportError:
    HAS_ICMPLIB = False

@dataclass
class PingResult:
    target_ip: str
    is_available: bool
    latency_avg_ms: float
    latency_min_ms: float
    latency_max_ms: float
    jitter_ms: float
    packet_loss_pct: float
    timestamp: float

def _calculate_jitter(rtts: list[float]) -> float:
    """Calcula o jitter médio conforme RFC 3550"""
    if len(rtts) < 2:
        return 0.0
    diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
    return round(statistics.mean(diffs), 3)

def _ping_system_fallback(target_ip: str, count: int = 5) -> PingResult:
    """Fallback usando o comando ping nativo do sistema operacional"""
    cmd = ["ping", "-c", str(count), "-W", "1", target_ip]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=count * 2)
        output = proc.stdout
        
        # Regex para extrair RTTs individuais: "time=XX.X ms"
        rtts = [float(m) for m in re.findall(r"time=([0-9.]+)\s*ms", output)]
        
        # Regex para perda de pacotes
        loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*packet loss", output)
        packet_loss = float(loss_match.group(1)) if loss_match else (0.0 if rtts else 100.0)
        
        if rtts:
            latency_avg = round(statistics.mean(rtts), 2)
            latency_min = round(min(rtts), 2)
            latency_max = round(max(rtts), 2)
            jitter = _calculate_jitter(rtts)
            is_avail = packet_loss < 100.0
        else:
            latency_avg = 0.0
            latency_min = 0.0
            latency_max = 0.0
            jitter = 0.0
            is_avail = False
            packet_loss = 100.0
            
        return PingResult(
            target_ip=target_ip,
            is_available=is_avail,
            latency_avg_ms=latency_avg,
            latency_min_ms=latency_min,
            latency_max_ms=latency_max,
            jitter_ms=jitter,
            packet_loss_pct=packet_loss,
            timestamp=time.time()
        )
    except Exception as e:
        return PingResult(
            target_ip=target_ip,
            is_available=False,
            latency_avg_ms=0.0,
            latency_min_ms=0.0,
            latency_max_ms=0.0,
            jitter_ms=0.0,
            packet_loss_pct=100.0,
            timestamp=time.time()
        )

def measure_ping(target_ip: str, count: int = 5) -> PingResult:
    """Executa medição de ping com fallback automático"""
    if HAS_ICMPLIB:
        try:
            host = icmp_ping(target_ip, count=count, interval=0.2, timeout=1.0, privileged=False)
            if host.is_alive and host.rtts:
                return PingResult(
                    target_ip=target_ip,
                    is_available=True,
                    latency_avg_ms=round(host.avg_rtt, 2),
                    latency_min_ms=round(host.min_rtt, 2),
                    latency_max_ms=round(host.max_rtt, 2),
                    jitter_ms=round(host.jitter, 3),
                    packet_loss_pct=round(host.packet_loss * 100, 2),
                    timestamp=time.time()
                )
            elif not host.is_alive:
                return PingResult(
                    target_ip=target_ip,
                    is_available=False,
                    latency_avg_ms=0.0,
                    latency_min_ms=0.0,
                    latency_max_ms=0.0,
                    jitter_ms=0.0,
                    packet_loss_pct=100.0,
                    timestamp=time.time()
                )
        except Exception:
            # Fallback para ping de subprocess caso falhe por permissão
            pass
            
    return _ping_system_fallback(target_ip, count)
