# Sistema de Agendamento para Manicure

## 1. Instalar Python
Instale Python 3.11+.

## 2. Abrir o projeto
No terminal, entre nesta pasta e execute:

```bash
python -m venv .venv
```

Windows:
```bash
.venv\Scripts\activate
```

macOS/Linux:
```bash
source .venv/bin/activate
```

## 3. Instalar dependências
```bash
pip install -r requirements.txt
```

## 4. Rodar
```bash
python app.py
```

Abra no navegador:
http://127.0.0.1:5000

Painel:
http://127.0.0.1:5000/admin

O banco SQLite `agendamentos.db` é criado automaticamente.

## 5. Colocar na internet
Para transformar isso em um link público, publique o projeto em um serviço de hospedagem Python, como Render, Railway ou PythonAnywhere. Depois você terá um endereço público para enviar às clientes.

## Próximas melhorias recomendadas
- Login e senha no painel administrativo.
- WhatsApp automático após a confirmação.
- Configuração de horários de trabalho por dia.
- Bloqueio de feriados/folgas.
- Pagamento ou sinal online.
- Personalização com nome, logo e Instagram do salão.
