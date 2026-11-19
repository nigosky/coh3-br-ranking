# CoH3 Brasil — Ranking Unificado BR

Projeto dedicado exclusivamente ao **Ranking Unificado Brasileiro** de **Company of Heroes 3 (Steam)**, separado do monitor de partidas (`nigosky/coh3-br-tracker`).

## O que este projeto faz
- Consulta diretamente a API oficial da Relic (`getPersonalStat`) para os **44 jogadores brasileiros monitorados / Veteranos (45 contas Steam)** nos modos oficiais (`1v1`, `2v2`, `3v3`, `4v4` × 4 facções), incluindo jogadores atualmente inativos (`inactive_fallback`).
- Unifica os modos em **3 rankings por média simples de ELO** exclusivamente com os Veteranos:
  - **1v1**
  - **2v2**
  - **Jogos em Equipe (`3v3 + 4v4`)**
- Renderiza tabelas em alta definição (`Pillow` + fontes `Roboto`/`DejaVu`/`Noto Sans CJK` + 257 bandeiras oficiais ISO PNG + avatares Steam + patente Veteranos), diferenciando visualmente jogadores inativos com cor de fundo própria, paginadas automaticamente e mantendo **1 mensagem fixa atualizada in-place** no Discord para os rankings individuais.
- Consome o repositório público **COH3 Stats Open Data** (`storage.coh3stats.com/matches/matches-{ts}.json`, com cache incremental em `ranking_cache.db`) para gerar o **Ranking Unificado de Times Brasileiros 2v2 Arranjados** (média ELO Eixo + Aliados com selo de lado, ambos os jogadores brasileiros ou Veteranos, mínimo de 3 partidas juntos por lado, Veteranos em destaque dourado e exclusão de times inativos há mais de 30 dias), publicado em **uma segunda mensagem fixa** no mesmo canal.
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
# Executar suíte de testes automatizados (ranking, ELO, PNG e segurança)
python3 -m unittest discover -s tests -v

# Varredura + exportação JSON/CSV + prévia dos PNGs sem enviar ao Discord
python3 run_ranking.py --dry-run --preview-dir ./preview

# Publicação real no Discord usando a variável de ambiente
DISCORD_RANKING_WEBHOOK_URL="https://discord.com/api/webhooks/..." python3 run_ranking.py --verify-discord --force
```
