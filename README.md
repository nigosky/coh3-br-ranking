# CoH3 Brasil — Ranking Unificado BR

Projeto dedicado exclusivamente ao **Ranking Unificado Brasileiro** de **Company of Heroes 3 (Steam)**, separado do monitor de partidas (`nigosky/coh3-br-tracker`).

## O que este projeto faz
- Varre automaticamente as **16 leaderboards oficiais da Relic** (`1v1`, `2v2`, `3v3`, `4v4` × 4 facções) filtrando jogadores do Brasil (`country=br`) + consulta direta (`getPersonalStat`) dos **44 jogadores brasileiros monitorados (45 contas Steam)**, incluindo jogadores atualmente inativos (`inactive_fallback`).
- Unifica os modos em **3 rankings por média simples de ELO**:
  - **1v1**
  - **2v2**
  - **Jogos em Equipe (`3v3 + 4v4`)**
- Renderiza tabelas em alta definição (`Pillow` + fontes `Roboto`/`DejaVu`/`Noto Sans CJK` + 257 bandeiras oficiais ISO PNG + avatares Steam + patente Veteranos) paginadas automaticamente e mantém **1 única mensagem fixa atualizada in-place** no Discord.
- Executa automaticamente **2x por dia** (**`00:01 BRT`** e **`12:00 BRT`**) via GitHub Actions, além de permitir disparo manual sob demanda (**Actions → Run workflow**).

## Configuração única do Secret no GitHub
1. No canal do Discord onde deseja exibir o **Ranking Unificado BR**, crie um Webhook (**Editar Canal → Integrações → Webhooks → Novo Webhook → Copiar URL do Webhook**).
2. Abra a página de Secrets deste repositório:
   `https://github.com/nigosky/coh3-br-ranking/settings/secrets/actions/new`
3. Crie o Secret:
   - **Name:** `DISCORD_RANKING_WEBHOOK_URL`
   - **Secret:** cole a URL do Webhook (`https://discord.com/api/webhooks/...`)
4. Clique em **Add secret** e, se quiser publicar imediatamente, vá em **Actions → CoH3 Brasil — Ranking Unificado BR → Run workflow**.

## Uso local (opcional)
```bash
# Varredura + exportação JSON/CSV + prévia dos PNGs sem enviar ao Discord
python3 run_ranking.py --dry-run --preview-dir ./preview

# Publicação real no Discord usando a variável de ambiente
DISCORD_RANKING_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 run_ranking.py --verify-discord --force
```
