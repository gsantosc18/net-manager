# Monitoramento Inteligente e Failover de Duas Conexões de Internet

Sistema desenvolvido para monitorar individualmente duas conexões de internet residenciais em um roteador **TP-Link Omada ER605 V2.20 (Standalone)**, calculando indicadores de qualidade, estabilidade e velocidade em tempo real com dashboard no Grafana.

---

## 🚀 Arquitetura da Solução

- **Isolamento de Conexões:** Configurado via *Policy Routing* e *IP Groups* no ER605:
  - Destinos da **Operadora 1 (WAN 1)** forçados exclusivamente pela interface `WAN`.
  - Destinos da **Operadora 2 (WAN 2)** forçados exclusivamente pela interface `WAN/LAN1`.
  - Servidores dedicados de teste de velocidade mapeados separadamente para cada operadora.
- **Motor de Monitoramento (`net-monitor`):**
  - Testes contínuos de resposta (Ping a cada 10 segundos).
  - Cálculo de estabilidade (Jitter RFC 3550) e perda de pacotes.
  - Teste periódico de velocidade real (Download e Upload).
  - Algoritmo de cálculo de pontuação (Health Score 0-100) com histerese e proteção contra flapping.
  - Exposição de métricas para o Prometheus.
- **Armazenamento de Métricas (`prometheus`):**
  - Coleta automática a cada 5 segundos com retenção histórica.
- **Painel Visual (`grafana`):**
  - Dashboard completo em português, pré-configurado e com atualização automática a cada 5 segundos.

---

## 📊 Acesso aos Serviços

| Serviço | Link de Acesso | Credenciais |
|---|---|---|
| **Painel Grafana** | [http://localhost:3030](http://localhost:3030) | Usuário: `admin` / Senha: `admin` |
| **Prometheus** | [http://localhost:9090](http://localhost:9090) | Acesso direto |
| **Métricas Brutas** | [http://localhost:8010/metrics](http://localhost:8010/metrics) | Acesso direto |

---

## 🛠️ Comandos Úteis

### Ver logs em tempo real
```bash
docker compose logs -f net-monitor
```

### Reiniciar o sistema
```bash
docker compose restart
```

### Parar o sistema
```bash
docker compose down
```

### Iniciar em segundo plano
```bash
docker compose up -d
```
