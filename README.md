# Bot de Verificação Sexy Prime

Bot Telegram com verificação manual assistida por OCR e biometria facial.

## O que foi mantido

- Fluxo de termos, gênero, indicação, data, documento e vídeo.
- Integração de indicação com o site.
- Painel administrativo, administradores e bloqueios.
- Aprovação, rejeição e bloqueio exclusivamente manuais.
- Webhook e health check para Render.
- Lembrete de verificação pendente.

## Novas validações

- Idade mínima configurada em 20 anos.
- OCR do RG/documento com Amazon Textract.
- Comparação do rosto do documento com o rosto principal de vários quadros do vídeo usando Amazon Rekognition.
- Verificação auxiliar da presença do documento no vídeo.
- Relatório técnico enviado ao grupo antes da decisão humana.
- Falha na AWS não derruba o bot: a solicitação segue para revisão manual obrigatória.

O bot nunca aprova automaticamente. O resultado automático é apenas um auxílio para os administradores.

## Arquivo executado

Use exclusivamente:

```bash
python bot_sexy.py
```

`bot_sexy_admin.py` foi mantido apenas como cópia da versão anterior e não possui OCR/biometria.

## Variáveis do Render

Copie as chaves do arquivo `.env.example` para a área Environment do Render. Nunca publique valores reais no GitHub.

Obrigatórias para o bot:

- `BOT_TOKEN`
- `WEBHOOK_URL`
- `OWNER_ID`
- `REFERRAL_API_KEY`

Obrigatórias para OCR e biometria:

- `BIOMETRIC_ENABLED=true`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION=us-east-1`

Configurações já prontas:

- `MINIMUM_AGE=20`
- `FACE_SIMILARITY_THRESHOLD=85`
- `MAX_VIDEO_FRAMES=5`

## AWS

Crie um usuário IAM exclusivo para o bot e aplique somente a política disponível em `aws-iam-policy.json`. Ela permite apenas:

- `textract:DetectDocumentText`
- `rekognition:DetectFaces`
- `rekognition:CompareFaces`

Não é necessário criar bucket S3. As imagens são enviadas diretamente às APIs e o vídeo permanece temporariamente no servidor apenas durante a extração dos quadros.

## Render

- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot_sexy.py`
- Health Check Path: `/`

Depois de cadastrar as variáveis, use **Manual Deploy > Deploy latest commit** e teste o fluxo completo com um documento e um vídeo de teste autorizados.

## Resultado no grupo

O grupo recebe:

- Data e idade informadas.
- Data e idade lidas no documento.
- Confirmação se as datas coincidem.
- Confiança média do OCR.
- Similaridade facial e limite configurado.
- Quantidade de quadros com rosto detectado.
- Indicação se o documento parece estar visível no vídeo.
- Resultado `APTO PARA DECISÃO MANUAL` ou `REVISÃO MANUAL OBRIGATÓRIA`.

Mesmo quando todos os testes passam, um administrador precisa clicar manualmente em Aprovar, Rejeitar ou Bloquear.
