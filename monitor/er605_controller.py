import json
import logging
import ssl
import time
import urllib.parse
import urllib.request
import http.cookiejar
from typing import Dict, Optional, Tuple

logger = logging.getLogger("er605_controller")

class ER605Controller:
    """
    Controlador nativo para TP-Link ER605 V2.20 (Standalone).
    Interage diretamente com a API JSON/LuCI do roteador usando criptografia RSA nativa.
    """

    def __init__(self, host: str = "192.168.10.1", username: str = "myhouse", password: str = "24865310@Asd"):
        self.host = host
        self.username = username
        self.password = password
        self.stok: Optional[str] = None
        self._cookie_jar = http.cookiejar.CookieJar()
        
        # Contexto SSL para ignorar certificado autoassinado do roteador local
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
            urllib.request.HTTPSHandler(context=self._ctx)
        )

    def _get_headers(self, is_auth: bool = False) -> Dict[str, str]:
        ref_page = "index.html" if is_auth else "login.html"
        return {
            "Referer": f"https://{self.host}/webpages/{ref_page}",
            "Origin": f"https://{self.host}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
        }

    def login(self) -> bool:
        """Autentica no ER605 obtendo token de sessão (stok) e cookie sysauth"""
        try:
            # 1. Obter uptime do roteador (usado como salt da senha)
            req_lang = urllib.request.Request(
                f"https://{self.host}/cgi-bin/luci/;stok=/locale?form=lang",
                data=b"operation=read",
                headers=self._get_headers(is_auth=False)
            )
            resp_lang = json.loads(self._opener.open(req_lang, timeout=8).read())
            uptime = resp_lang.get("result", {}).get("uptime", 0)

            # 2. Obter chave pública RSA do roteador
            data_get = urllib.parse.urlencode({"data": json.dumps({"method": "get"})}).encode("utf-8")
            req_key = urllib.request.Request(
                f"https://{self.host}/cgi-bin/luci/;stok=/login?form=login",
                data=data_get,
                headers=self._get_headers(is_auth=False)
            )
            resp_key = json.loads(self._opener.open(req_key, timeout=8).read())
            mod_hex, exp_hex = resp_key["result"]["password"]
            n = int(mod_hex, 16)
            e = int(exp_hex, 16)

            # 3. Criptografar senha com RSA (NoPadding com preenchimento em zeros até 128 bytes)
            raw = f"{self.password}_{uptime}".encode("utf-8")
            pad = b"\x00" * (128 - len(raw))
            m = int.from_bytes(raw + pad, byteorder="big")
            c = pow(m, e, n)
            c_hex = f"{c:0256x}"

            # 4. Enviar credenciais
            login_payload = {
                "method": "login",
                "params": {
                    "username": self.username,
                    "password": c_hex
                }
            }
            data_login = urllib.parse.urlencode({"data": json.dumps(login_payload)}).encode("utf-8")
            req_login = urllib.request.Request(
                f"https://{self.host}/cgi-bin/luci/;stok=/login?form=login",
                data=data_login,
                headers=self._get_headers(is_auth=False)
            )
            resp_login = json.loads(self._opener.open(req_login, timeout=8).read())
            if resp_login.get("error_code") == "0":
                self.stok = resp_login.get("result", {}).get("stok")
                logger.info("Autenticação no ER605 realizada com sucesso.")
                return True
            else:
                logger.error(f"Falha ao autenticar no ER605: {resp_login}")
                return False
        except Exception as ex:
            logger.error(f"Erro na conexão com ER605 durante login: {ex}")
            return False

    def _api_post(self, path: str, payload: dict, retries: int = 1) -> Optional[dict]:
        """Executa chamada autenticada na API do ER605 com re-login automático em caso de timeout"""
        if not self.stok:
            if not self.login():
                return None

        url = f"https://{self.host}/cgi-bin/luci/;stok={self.stok}{path}"
        data = urllib.parse.urlencode({"data": json.dumps(payload)}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._get_headers(is_auth=True))

        try:
            resp = json.loads(self._opener.open(req, timeout=10).read())
            # Se a sessão expirou (código 704 ou 1014), renovar e tentar novamente
            if resp.get("error_code") in (704, "704", 1014, "1014") and retries > 0:
                logger.info("Sessão do ER605 expirou. Renovando credenciais...")
                self.stok = None
                if self.login():
                    return self._api_post(path, payload, retries=retries - 1)
            return resp
        except Exception as ex:
            logger.error(f"Erro na requisição para {path}: {ex}")
            if retries > 0:
                self.stok = None
                if self.login():
                    return self._api_post(path, payload, retries=retries - 1)
            return None

    def get_policy_routes(self) -> Tuple[Optional[dict], Optional[int]]:
        """Lê a tabela de Policy Routing e localiza a regra ROUTE_DEFAULT"""
        resp = self._api_post("/admin/policy_route?form=policy_route", {"method": "get", "params": {}})
        if not resp or resp.get("error_code") != "0":
            return None, None

        rules = resp.get("result", [])
        for i, rule in enumerate(rules):
            if rule.get("name") == "ROUTE_DEFAULT":
                return rule, i
        return None, None

    def get_active_primary_wan(self) -> Optional[str]:
        """Retorna 'wan1' ou 'wan2' com base na interface ativa em ROUTE_DEFAULT"""
        rule, _ = self.get_policy_routes()
        if not rule:
            return None
        interfaces = rule.get("interfaces", "")
        if "WAN1" in interfaces and "WAN2" not in interfaces:
            return "wan1"
        elif "WAN2" in interfaces:
            return "wan2"
        return None

    def set_primary_wan(self, target_wan: str) -> bool:
        """
        Altera a interface da regra ROUTE_DEFAULT no ER605 e salva na memória flash.
        target_wan: 'wan1' (WAN) ou 'wan2' (WAN/LAN1)
        """
        wan_code = "WAN1" if target_wan.lower() == "wan1" else "WAN2"
        wan_label = "WAN" if wan_code == "WAN1" else "WAN/LAN1"

        rule, idx = self.get_policy_routes()
        if not rule or idx is None:
            logger.error("Regra ROUTE_DEFAULT não encontrada no ER605 para alteração!")
            return False

        current_wan = "wan1" if rule.get("interfaces") == "WAN1" else "wan2"
        if current_wan == target_wan.lower():
            logger.info(f"ER605 já está operando com {target_wan.upper()} ({wan_label}) como rota primária. Nenhuma alteração necessária.")
            return True

        logger.warning(
            f"⚡ ALTERANDO CONEXÃO PRINCIPAL NO ER605: {current_wan.upper()} ➔ {target_wan.upper()} ({wan_label})"
        )

        new_rule = dict(rule)
        new_rule["interfaces"] = wan_code
        new_rule["comment"] = f"Default route via {wan_code}"

        payload = {
            "method": "set",
            "params": {
                "index": idx,
                "old": rule,
                "new": new_rule,
                "key": f"key-{idx}"
            }
        }

        # 1. Aplicar a nova rota
        resp_set = self._api_post("/admin/policy_route?form=policy_route", payload)
        if not resp_set or resp_set.get("error_code") != "0":
            logger.error(f"Erro ao aplicar nova rota no ER605: {resp_set}")
            return False

        # 2. Salvar na memória Flash permanentemente
        resp_save = self._api_post("/userconfig?form=cfg_save", {"method": "set", "id": 1})
        if not resp_save or resp_save.get("error_code") != "0":
            logger.warning(f"Rota aplicada, mas resposta de salvamento em Flash retornou: {resp_save}")

        logger.info(f"✅ ROTA PRINCIPAL NO ER605 ALTERADA COM SUCESSO PARA {target_wan.upper()} ({wan_label})!")
        return True
