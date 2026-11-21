import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("speedtest_collector")

# Mapeamento do servidor dedicado para cada WAN (roteado por Policy Routing no ER605)
WAN_SPEEDTEST_SERVERS = {
    "wan1": {
        "url": "http://velocimetro-blm.virtua.com.br:8080/speedtest/upload.php",
        "lat": "-1.4558",
        "lon": "-48.5039",
        "name": "Belém",
        "country": "Brazil",
        "cc": "BR",
        "sponsor": "Claro Fibra - BLM",
        "id": "21853",
        "host": "velocimetro-blm.virtua.com.br:8080",
        "d": 0.0
    },
    "wan2": {
        "url": "http://speedtest.lusotec.com.br:8080/speedtest/upload.php",
        "lat": "-1.4558",
        "lon": "-48.5039",
        "name": "Belém",
        "country": "Brazil",
        "cc": "BR",
        "sponsor": "Movih Telecom",
        "id": "66188",
        "host": "speedtest.lusotec.com.br:8080",
        "d": 0.0
    }
}

class SpeedtestResult:
    def __init__(self, download_mbps: float = 0.0, upload_mbps: float = 0.0, ping_ms: float = 0.0, timestamp: float = 0.0):
        self.download_mbps = download_mbps
        self.upload_mbps = upload_mbps
        self.ping_ms = ping_ms
        self.timestamp = timestamp

class SpeedtestManager:
    def __init__(self):
        self.latest_results: Dict[str, SpeedtestResult] = {}
        self.is_running = False
        self._lock = threading.Lock()

    def run_all_async(self):
        """Executa o teste para ambas as operadoras em background sequencialmente"""
        thread = threading.Thread(target=self._run_all, daemon=True)
        thread.start()

    def _run_all(self):
        with self._lock:
            if self.is_running:
                logger.info("Teste de velocidade já em andamento. Aguardando conclusão...")
                return
            self.is_running = True

        try:
            for wan_id, server_info in WAN_SPEEDTEST_SERVERS.items():
                self._measure_single_wan(wan_id, server_info)
                time.sleep(2)
        finally:
            with self._lock:
                self.is_running = False

    def _measure_single_wan(self, wan_id: str, server_info: dict):
        logger.info(f"Iniciando teste de velocidade para {wan_id.upper()} usando servidor dedicado {server_info['sponsor']} ({server_info['host']})...")
        try:
            import speedtest
            s = speedtest.Speedtest()
            s.get_config()
            s.servers = {0.0: [server_info]}
            s.get_best_server()
            
            # Download
            s.download()
            # Upload
            s.upload()
            
            res = s.results.dict()
            down = round(res.get("download", 0) / 1_000_000, 2)
            up = round(res.get("upload", 0) / 1_000_000, 2)
            ping = round(res.get("ping", 0), 2)
            
            with self._lock:
                self.latest_results[wan_id] = SpeedtestResult(
                    download_mbps=down,
                    upload_mbps=up,
                    ping_ms=ping,
                    timestamp=time.time()
                )
            logger.info(
                f"✅ Velocidade {wan_id.upper()}: Download={down} Mbps | Upload={up} Mbps | Latência={ping}ms"
            )
        except Exception as e:
            logger.warning(f"Erro ao medir velocidade para {wan_id}: {e}")

    def get_result(self, wan_id: str) -> Optional[SpeedtestResult]:
        with self._lock:
            return self.latest_results.get(wan_id)
