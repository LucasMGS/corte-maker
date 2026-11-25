# corte-maker

Recebe o link de uma live/vídeo (YouTube, Twitch, Kick...) ou um arquivo local, acha os melhores momentos e entrega cortes verticais 1080x1920 já editados (fundo desfocado, título, legenda palavra a palavra, áudio normalizado), cada um com um `.txt` de título, descrição e hashtags para colar no YouTube Shorts / TikTok.

## Instalação

Feito e testado no Windows, com placa de vídeo NVIDIA (sem ela a transcrição roda no processador, bem mais devagar). Precisa do [uv](https://docs.astral.sh/uv/getting-started/installation/), que baixa o Python e as dependências:

```
git clone https://github.com/SEU-USUARIO/corte-maker.git
cd corte-maker
uv sync
copy .env.example .env
```

O ffmpeg vem junto com as dependências (`imageio-ffmpeg`), e a voz local do narrador é baixada na primeira narração.

## Configuração

Tudo o que é seu fica no arquivo `.env` da raiz (copiado do `.env.example`, que explica cada opção). Ele nunca vai para o git; cada opção também pode vir de uma variável de ambiente com o mesmo nome.

| Opção | Para quê |
|---|---|
| `ANTHROPIC_API_KEY` | acesso ao Claude pela API, paga por uso. Sem ela, usa o Claude Code instalado na máquina (`claude`) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | narração com a voz paga da ElevenLabs (opcional) |
| `CORTES_MARCA` | assinatura do seu canal no topo de cada corte, por exemplo `@SeuCanal` (vazio: sem marca) |
| `CORTES_SITE_URL` | site do seu app (termos e privacidade), mostrado nas instruções do TikTok |
| `PORT` | porta da interface (padrão: 8765) |

As credenciais do YouTube e do TikTok ficam na pasta `credenciais\`, que também não vai para o git (veja [Publicar no YouTube](#publicar-no-youtube)). O que é gerado ao usar (`saida\`, `trabalho\`, `voz\`, `canal\`, `gastos.csv`) fica só na sua máquina.

## Interface

Dê dois cliques em `interface.bat` (ou rode `.\interface` na pasta do projeto). Abre no navegador, em `http://127.0.0.1:8765`:

- aba **Criar** (`#/criar`): cole links ou arraste um vídeo, escolha quantidade, duração, enquadramento e o acabamento (marca, narrador, voz, legendas). O seletor no alto troca entre **Vídeo gravado** e **Live no ar** (`#/criar/live`). Ao lado fica a fila, com o progresso de cada vídeo gravado (um por vez, por causa da placa de vídeo);
- aba **Lives** (`#/lives`): as lives acompanhadas lado a lado, com um campo para adicionar outra, a prévia da gravação, o corte manual, botão para remover e, em cada uma, o que procurar, a nota mínima e o enquadramento, que podem ser mudados com a live no ar;
- aba **Cortes** (`#/cortes`): a galeria dos cortes prontos, com busca por título ou canal, filtro por origem (de vídeos ou de lives), a linha do tempo de onde cada um saiu, prévia ao passar o mouse e um visor com botões de copiar título e legenda;
- aba **Compilações** (`#/compilacoes`): os vídeos horizontais prontos, os que estão sendo montados e os temas propostos.

Cada aba, cada pasta de cortes e cada corte aberto tem endereço próprio (por exemplo `#/cortes/<pasta>/3`), então o voltar do navegador funciona e dá para guardar ou recarregar a página no ponto em que estava. O número em cada aba mostra o que está acontecendo nela (lives no ar, vídeos na fila, cortes e compilações prontos).

A página só funciona enquanto a janela do `interface.bat` estiver aberta, e só é acessível deste computador.

## Live no ar (modo ao vivo)

Na interface, cole o link na aba **Lives** (ou na aba **Criar**, em **Live no ar**, para escolher as opções antes). No terminal:

```
.\cortes --ao-vivo "https://www.twitch.tv/canal" --limiar 75
```

O programa grava a live em pedaços e, a cada 2 min e meio de transmissão, analisa o trecho novo. Cada momento com nota igual ou maior que a exigência vira corte na hora, de 3 a 5 minutos depois de acontecer. Para parar, use o botão **Parar de monitorar** (ou Ctrl+C no terminal): ele fecha a gravação, analisa o que faltou e encerra. Se a live acabar, encerra sozinho.

- A gravação já analisada é apagada do disco conforme a live avança; só os cortes ficam.
- Na interface dá para acompanhar várias lives ao mesmo tempo, sem limite de quantidade (um link por linha), e a fila de vídeos gravados segue andando em paralelo. Todas disputam a mesma placa de vídeo e a mesma cota da IA, então cada corte demora mais para sair.
- Julgar se um momento é engraçado depende da IA. Sem acesso ao Claude, só picos de volume viram corte, o que é um critério bem mais fraco.
- Cada análise é uma chamada ao Claude: uma live de 4 horas dá perto de 100 chamadas, o que pesa no limite da assinatura.
- **Triagem rápida** (`--triagem`, ou a chave de mesmo nome nas opções da interface): antes de cada análise, um modelo barato (Claude Haiku) lê o trecho e dá uma nota; a análise completa só roda quando essa nota chega a 30 pontos da exigência. Num teste com 100 trechos de uma final de CS, isso dispensou cerca de metade das análises completas, mas deixou passar 1 dos 5 melhores momentos do vídeo. Vem desligada: ligue quando o limite da assinatura apertar, por exemplo com várias lives ao mesmo tempo.

## Linha de comando

Na pasta do projeto:

```
.\cortes "https://www.youtube.com/watch?v=..."
.\cortes "C:\videos\live.mp4" -n 8 --contexto "gameplay de GTA 6, foco em humor e fails"
.\cortes LINK1 LINK2 arquivo.mp4
```

Os cortes saem em `saida\<título do vídeo>\`, numerados do melhor para o pior (`01 - ...mp4` + `01 - ....txt`).

| Opção | O que faz |
|---|---|
| `-n 5` | quantos cortes por vídeo |
| `--min 20 --max 60` | duração de cada corte, em segundos |
| `--layout blur` | `blur` (padrão): vídeo aproximado sobre fundo desfocado · `cheio`: tela cheia cortando as laterais · `inteiro`: vídeo completo sobre fundo desfocado · `preto`: vídeo completo no meio, fundo preto, sem desfoque · `camera`: câmera do streamer em cima e o jogo embaixo (precisa de `--camera`) |
| `--narrar-na-hora` | grava a voz do narrador em todo corte já na geração. Sem isto (o padrão), a IA só escreve o comentário, e a voz é gravada quando você abre o corte na interface e clica em *Adicionar narração*. Cada corte guarda o trecho original em `_fontes/`, e é isso que permite trocar o enquadramento ou pôr/tirar a narração depois de pronto |
| `--camera X,Y,L,A` | com `--layout camera`: onde fica a câmera no vídeo, em frações do quadro (ex.: `0.75,0.7,0.25,0.3` = canto inferior direito). Na interface, o botão *Marcar a câmera* tira uma foto do vídeo para você arrastar em volta dela |
| `--contexto "..."` | dica para a IA (jogo, streamer, tipo de momento que você quer) |
| `--inicio 1:30:00 --fim 2:00:00` | em links, baixa e analisa só esse trecho |
| `--idioma pt` | fixa o idioma falado (padrão: detecta) |
| `--sem-ia` | escolhe só pelos picos de áudio, sem Claude |
| `--sem-legenda` | não queima legenda |
| `--comentario pt` | no fim de cada corte o vídeo para e uma voz neural narra um comentário escrito pela IA, com uma pergunta ao público, em `pt` (padrão) ou `en`; `nao` desliga. Título e descrição saem no mesmo idioma. Na primeira vez baixa o programa de voz e a voz para `voz\` (80 a 140 MB) |
| (voz) | com `ELEVENLABS_API_KEY` no ambiente ou no arquivo `.env` da raiz, a narração usa a ElevenLabs (paga por caractere, mais natural); sem a chave, ou se ela falhar, usa a voz local gratuita. `ELEVENLABS_VOICE_ID` e `ELEVENLABS_MODEL` no `.env` trocam a voz e o modelo |
| `--voz liam` | voz do narrador: `liam`, `will`, `brian`, `eric` (ElevenLabs) ou `faber`, `cadu`, `jeff`, `edresson` (locais). Na interface há um botão para ouvir cada uma antes de escolher |
| `--abertura` | o narrador também fala antes do corte, com o vídeo parado no primeiro quadro |
| `--sem-traducao` | legenda só no idioma falado. O padrão é ter embaixo uma segunda linha traduzida (português para inglês, outros idiomas para português), o que custa uma chamada a mais à IA por corte |
| `--manter-pausas` | não mexe nos silêncios. O padrão é tirar o silêncio do começo e do fim do corte e encurtar para menos de meio segundo as pausas mudas de 1 s ou mais (microfone mudo, tela de carregamento sem som). Pausa na fala com o som do jogo por cima fica, porque ali pode estar a ação |
| (gancho) | com a escolha por IA, cada corte abre com uma frase curta e grande na tela por 2,5 s, no lugar do título, que entra em seguida. A frase também vai para o `.txt` do corte |
| `--marca "@SeuCanal"` | assinatura do canal, pequena no topo de cada corte (padrão: `CORTES_MARCA` do `.env`; `--marca ""` tira) |

## Compilações (vídeo normal, horizontal)

Junta cortes que já estão na biblioteca num vídeo mais longo, 1920x1080, com um tema único (por exemplo "encrencas com a polícia" ou "mistérios do mapa"), para publicar como vídeo normal do YouTube. Na interface, aba **Compilações**: **Propor temas** mostra os grupos que a IA montou e **Gerar vídeo** monta o escolhido. No terminal:

```
.\compilar                 mostra os temas propostos e pergunta qual gerar
.\compilar 2               gera o tema 2 da lista
.\compilar --pedido "bugs e bizarrices do mapa"
```

- **Tema:** a IA lê título, descrição, hashtags, motivo e nota de todos os cortes de `saida\` e devolve grupos de 4 a 12 cortes com título, descrição e ordem (o mais forte primeiro, o segundo mais forte por último). É uma chamada ao Claude por proposta; a resposta fica guardada em `trabalho\compilacoes\temas.json`, então listar de novo e gerar o vídeo não gastam nada. `--de-novo` pede outra proposta, `--pedido` diz o tema que você quer.
- **Imagem de cada momento,** do melhor para o pior material que houver:
  1. o trecho original baixado de novo pelo link, em 16:9 de verdade e sem nada queimado na imagem. Só vale para cortes de vídeos gravados: num corte de live, os tempos contam do começo do monitoramento, e não dá para achar o trecho na gravação que a plataforma publica depois;
  2. a faixa do corte vertical onde está o vídeo (enquadramentos *Aproximado* e *Inteiro*), recortada e ampliada para a tela toda. Somem o fundo desfocado, o título e a legenda vertical. A imagem vem de 1080 pixels de largura, então fica um pouco menos nítida que o original;
  3. cortes em *Tela cheia* não têm faixa para recortar: entram inteiros, na vertical, sobre fundo desfocado.
  `--so-prontos` pula a tentativa de baixar o original.
- **Narrador:** as falas do narrador que os cortes têm (o comentário com a pergunta no fim e, se houver, a fala de abertura) ficam de fora: de cada corte entra só o vídeo, e um momento emenda no outro pela cartela. O programa acha a fala pela imagem, que fica parada e mais escura enquanto o narrador fala. `--com-narrador` mantém as falas.
- **Montagem:** o primeiro momento abre o vídeo direto; entre um momento e outro entra uma cartela de 2 segundos com o número e o título do seguinte. Cada momento mostra o título no canto nos primeiros 5 segundos e, o tempo todo, a marca do canal e o crédito de quem é o vídeo. O som dos trechos originais é normalizado como nos cortes; o dos cortes prontos já está.
- **Saída,** em `saida\COMPILAÇÃO - <tema>\`: o vídeo; `miniatura.jpg` (1920x1080, um quadro forte com até 3 palavras), a mesma sem texto e até duas opções tiradas de outros momentos, porque o programa escolhe o quadro por nitidez, cor e volume e quem olha escolhe melhor; e um `.txt` com título e descrição prontos para colar, com os capítulos (`0:00 ...`) e os créditos de cada canal.
- **Três títulos para o teste A/B:** cada tema vem com o título principal e mais dois com estratégias diferentes (um de busca, direto, com o nome do jogo na frente; um de curiosidade). Os três saem no `.txt`, marcados A, B e C, para colar no "Testar e comparar" do YouTube Studio, e o visor da interface tem um botão de copiar para cada um. O teste do YouTube vale para vídeos normais, não para Shorts.

- **Miniatura escolhida olhando:** `.\compilar --quadros "COMPILAÇÃO - <tema>"` grava uma folha de quadros por momento em `trabalho\miniaturas\`, e `.\compilar --miniatura "COMPILAÇÃO - <tema>" --momento 2 --tempo 12.5 --texto "Parece filme"` grava a miniatura com o quadro daquele instante como mais uma opção na pasta da compilação.

## Publicar no YouTube

No visor de cada corte e de cada compilação há o botão **Publicar no YouTube**. Ele abre uma janela para conferir o que vai subir: o título (numa compilação, os três do teste A/B para escolher), a miniatura (as opções da pasta) e quando o vídeo fica público: **Privado** (você libera depois no YouTube Studio), **Agendar** (dia e hora) ou **Público** na hora. O andamento do envio fica no canto da tela e, no fim, o botão vira **Ver no YouTube**.

- Sobem o vídeo, o título, a descrição (com capítulos, créditos e hashtags), as hashtags como etiquetas, a categoria Jogos e, nas compilações, a miniatura escolhida. Corte vertical curto vira Short sozinho.
- O teste A/B de título e de miniatura ("Testar e comparar") não existe na API: o vídeo sobe com o título e a miniatura escolhidos, e o teste continua sendo ligado à mão no YouTube Studio.
- Miniatura personalizada só é aceita em canal verificado por telefone (`youtube.com/verify`). Se o YouTube recusar, o vídeo sobe do mesmo jeito e a página avisa.
- Cada publicação fica anotada em `canal\publicados.json` e no bloco do vídeo em `canal\resultados.md`.
- Se a conexão cair no meio, o envio pergunta ao YouTube até onde chegou e continua dali.

## Enviar pro TikTok

No visor de cada corte há o botão **Enviar pro TikTok**. Na primeira vez, a janela explica como criar o app em `developers.tiktok.com` (produtos Login Kit, plataforma Desktop, com o retorno `http://127.0.0.1:*/tiktok/retorno/`, e Content Posting API) e pede a Client key e o Client secret, que ficam em `credenciais\tiktok.json`. Depois é só conectar a conta uma vez.

- **Rascunho** (padrão): o vídeo chega na caixa de entrada do app do TikTok e você põe legenda e som e publica por lá. A legenda do corte é copiada na hora do envio. O TikTok aceita até 5 rascunhos pendentes por dia.
- **Direto**: posta na hora com a legenda. Enquanto o TikTok não auditar o app, só como privado.
- O que foi enviado fica anotado em `canal\publicados_tiktok.json`.

### A credencial do Google (uma vez)

O programa publica como você, com uma credencial que só você pode criar. Leva uns 15 minutos:

1. Entre em `console.cloud.google.com` com a conta do canal, crie um projeto (qualquer nome) e, na **Biblioteca de APIs**, ative a **YouTube Data API v3**.
2. Abra o **Google Auth Platform** (a antiga tela de consentimento OAuth) e crie primeiro o **Branding**, no botão **Começar**: nome do app (qualquer um), o seu e-mail como suporte e como contato, e o público **Externo**. Deixe em branco o logotipo e tudo de "App domain". Depois, na página **Público-alvo** (Audience), **não** clique em **Publicar app**: em **Usuários de teste** (Test users), adicione o e-mail da conta Google que administra o canal (se o canal for de uma conta de marca, é o e-mail com que você entra no YouTube Studio). O app fica "em teste", e nesse modo o Google vence a autorização a cada 7 dias: quando isso acontece, a janela de publicar volta para **Conectar ao YouTube**, e reconectar leva uns segundos. Para tirar o app do modo de teste, o Google exige página inicial e política de privacidade publicadas num domínio seu, cadastrado em domínios autorizados.
3. Na página **Clientes** (Clients), clique em **Criar cliente**, escolha o tipo **App para computador** (Desktop app) e baixe o arquivo JSON.
4. Salve o arquivo, sem mudar o nome, na pasta `credenciais\` do projeto (o botão **Abrir a pasta** da janela de publicar leva até lá).
5. Clique em **Publicar no YouTube** em qualquer vídeo e depois em **Conectar ao YouTube**. O Google abre numa aba, avisa que o app não foi verificado (é o seu próprio app: **Avançado** e **Acessar**), e pergunta se ele pode enviar vídeos para o canal. A autorização fica em `credenciais\youtube_token.json`; a sua senha nunca passa pelo programa.

Para desconectar, use **Desconectar o canal** na janela de publicar, ou tire o acesso do app em `myaccount.google.com/permissions`.

## Como escolhe os momentos

1. Transcreve o áudio com tempo por palavra (faster-whisper, na GPU).
2. Mede o volume ao longo do vídeo e marca os picos (gritos, risadas, ação).
3. Manda a transcrição com os picos marcados para o Claude, que devolve os trechos, nota de 0 a 100, título, descrição e hashtags.
4. Sem acesso ao Claude, cai para os picos de áudio (funciona, mas com títulos genéricos).

O acesso ao Claude usa, nesta ordem: a `ANTHROPIC_API_KEY` do ambiente ou do `.env` (API paga por uso) ou o Claude Code instalado na máquina (`claude`, com a sua assinatura). Se aparecer "login expirado", abra um terminal, rode `claude` e digite `/login`.

## Observações

- Primeira execução baixa o modelo de transcrição (~1,6 GB).
- Espaço em disco: de um link, só o áudio é baixado para a análise (dezenas de MB por hora) e depois só os trechos de vídeo que viram corte. Ao terminar, tudo isso é apagado; em `trabalho\` ficam apenas a transcrição e a análise (alguns KB), que aceleram uma nova rodada do mesmo vídeo. Se uma rodada falhar no meio, o que ela baixou fica em `trabalho\`, que pode ser apagada quando quiser.
- `--baixar-tudo` volta ao comportamento antigo de baixar o vídeo inteiro (também apagado ao terminar).
- Um link de live que ainda está no ar só funciona no modo ao vivo.
- Live ainda no ar: o download grava até ela acabar. O ideal é rodar depois que a gravação estiver disponível.
- Poste cortes só de conteúdo seu ou de criadores que permitem cortes, com os créditos (o `.txt` já traz o canal e o link de origem).

## Licença

[MIT](LICENSE).
