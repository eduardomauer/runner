# MILK Drive → Box Migrator

Automação para copiar ficheiros existentes do Google Drive para Box sem alterar os originais.

## Regras operacionais

- Não cria conteúdo novo a partir dos ficheiros do Drive.
- Não reescreve, resume, corrige ou altera o conteúdo dos ficheiros.
- Não apaga, move ou renomeia nada no Google Drive.
- Não apaga nada no Box.
- Preserva nomes e extensões quando possível.
- Salta duplicados já existentes no Box.
- Salta nomes que pareçam conter credenciais, chaves, tokens ou passwords.
- Exporta Google Docs/Sheets/Slides para formatos Office apenas quando necessário para transporte.
- Gera relatório CSV como artifact do GitHub Actions.

## Autorização necessária

Esta automação só executa de forma completa quando os seguintes segredos forem adicionados ao repositório GitHub em Settings → Secrets and variables → Actions:

### Google Drive

- GOOGLE_CLIENT_ID
- GOOGLE_CLIENT_SECRET
- GOOGLE_REFRESH_TOKEN
- DRIVE_SOURCE_FOLDER_ID

### Box

- BOX_CLIENT_ID
- BOX_CLIENT_SECRET
- BOX_ACCESS_TOKEN
- BOX_REFRESH_TOKEN
- BOX_DESTINATION_FOLDER_ID

## Execução

Depois de configurar os segredos, use Actions → MILK Drive to Box Migration → Run workflow.

Primeiro rode com dry_run = true. Depois rode com dry_run = false.
