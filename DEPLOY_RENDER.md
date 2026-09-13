# Publicar no Render

1. Crie um repositório no GitHub chamado `agendamento-manicure`.
2. Envie todos os arquivos desta pasta para o repositório.
3. No Render, escolha New > Web Service e conecte o repositório.
4. Use Build Command: `pip install -r requirements.txt`.
5. Use Start Command: `gunicorn app:app`.
6. Escolha o plano Free para teste.
7. Ao terminar, o Render fornece uma URL HTTPS pública `onrender.com`.

Atenção: este projeto usa SQLite. Para uso real, é recomendado migrar os agendamentos para PostgreSQL antes de depender do sistema em produção.
