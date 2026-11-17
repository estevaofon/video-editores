"""Integra o video-editores com outros agentes: Codex (OpenAI), OpenCode, Kiro (AWS) e ZCode (z.ai).

Para cada agente, três coisas (ADR-12):
  1. skills   as mesmas SKILL.md do plugin, copiadas para a pasta que o agente lê, com `${CLAUDE_PLUGIN_ROOT}`
              trocado pelo caminho real dos scripts e um cabeçalho dizendo como executar fora do Claude Code;
  2. MCP      o servidor video-editores-mcp registrado no formato de configuração do agente;
  3. regras   um bloco em AGENTS.md (Codex, OpenCode, ZCode) ou um steering (Kiro) com as convenções do projeto.

Escopos: --projeto DIR (padrão: pasta atual) grava dentro do projeto; --usuario grava na configuração global.
Tudo idempotente: rodar de novo atualiza o que é nosso e não toca no resto do arquivo. O que é nosso é reconhecível:
todo port carimba a versão (`<!-- video-editores:port X.Y.Z -->`) e cada destino ganha um manifesto oculto
(`.video-editores.json`) com versão, raiz, lista de skills e dos agentes que dependem daquele destino — Codex,
OpenCode e ZCode dividem `.agents/skills` e AGENTS.md em projeto, então lá o cabeçalho é o neutro, o bloco soma as
chamadas dos três e o destino só é removido quando a lista de dependentes esvazia. `--mostrar` compara o instalado
com a versão atual e relata ports desatualizados; sobrescrever e remover só toca em portação do plugin — pasta
homônima alheia é preservada com aviso.

Pastas e arquivos (fontes: docs oficiais, nov/2026):
  Codex     skills .agents/skills/ ou ~/.agents/skills/ · MCP .codex/config.toml ou ~/.codex/config.toml [mcp_servers.x] · AGENTS.md
  OpenCode  skills .agents/skills/ (também .opencode/skills/) ou ~/.config/opencode/skills/ · MCP opencode.json mcp.servers.x · AGENTS.md
  Kiro      skills .kiro/skills/ ou ~/.kiro/skills/ (global só na IDE) · MCP .kiro/settings/mcp.json mcpServers.x · .kiro/steering/*.md
  ZCode     skills .agents/skills/ (projeto) ou ~/.zcode/skills/ · MCP ~/.zcode/cli/config.json mcp.servers.x (só global) · AGENTS.md

Uso: video-editores integrar codex|opencode|kiro|zcode|todos [--projeto DIR] [--usuario] [--raiz-midia DIR]
                               [--sem-skills] [--sem-mcp] [--sem-regras] [--remover] [--mostrar]
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import __version__, raiz_recursos

NOME = "video-editores"
GIT = "video-editores[mcp] @ git+https://github.com/fmnapoli/video-editores"
REPO = "https://github.com/fmnapoli/video-editores"
INI, FIM = f"<!-- {NOME}:inicio -->", f"<!-- {NOME}:fim -->"
MANIFESTO = f".{NOME}.json"
CARIMBO = f"<!-- {NOME}:port {__version__} -->"


# ---------------------------------------------------------------- comando do servidor MCP
def _efemero(p) -> bool:
    """Caminho dentro do cache do uv (ambiente de um `uvx`), que pode sumir: não serve para registrar em configuração."""
    s = str(p)
    return "/.cache/uv/" in s or "/archive-v0/" in s or "/uv/cache/" in s


def comando_mcp(raiz_midia: Path) -> list:
    """Como iniciar o servidor MCP nesta máquina: binário instalado (uv tool install) > checkout com uv > uvx do GitHub."""
    raiz_midia = str(raiz_midia)
    bin_ = shutil.which("video-editores-mcp")
    if bin_ and not _efemero(bin_):
        return [bin_, "--raiz", raiz_midia]
    rec = raiz_recursos()
    if (rec / "pyproject.toml").is_file() and shutil.which("uv"):
        return ["uv", "run", "--quiet", "--with", "mcp>=2", "--directory", str(rec), "python", "-m",
                "video_editores.mcp_server", "--raiz", raiz_midia]
    return ["uvx", "--from", GIT, "video-editores-mcp", "--raiz", raiz_midia]


# ---------------------------------------------------------------- cabeçalhos das skills portadas
CABECALHO = """> **Fora do Claude Code.** Esta skill vem do plugin [video-editores]({repo}) {versao}. Os scripts estão em
> `{raiz}` (os comandos abaixo já apontam para lá). Alternativas equivalentes: a CLI `video-editores <operação>`
> (`video-editores listar`) e as ferramentas do servidor MCP `video-editores` (`listar_operacoes`, `executar_operacao`,
> `tarefa_status`). Onde o texto fala em *subagentes*, execute em sequência ou use o mecanismo de paralelismo do seu
> agente; onde pede *aprovação*, pare e pergunte ao usuário antes de seguir.

"""

CABECALHO_ZCODE = """> **Fora do Claude Code (harness ZCode).** Esta skill vem do plugin [video-editores]({repo}) {versao}. Os scripts estão em
> `{raiz}` (os comandos abaixo já apontam para lá). Alternativas equivalentes: a CLI `video-editores <operação>`
> (`video-editores listar`) e as ferramentas do servidor MCP `video-editores` (`listar_operacoes`, `executar_operacao`,
> `tarefa_status`). Onde o texto fala em *subagentes*, use a ferramenta **Agent** do ZCode (despache em paralelo
> quando os trabalhos forem independentes) ou execute em sequência; onde pede *aprovação*, pare e pergunte ao usuário
> com a ferramenta **AskUserQuestion** antes de seguir.

"""


# ---------------------------------------------------------------- procedência e estado dos ports
def _cabeca(sk: Path) -> str:
    """Primeiras linhas do SKILL.md, onde moram carimbo e cabeçalho. Ilegível (encoding estranho) = não é nossa."""
    try:
        return "\n".join(sk.read_text(encoding="utf-8").splitlines()[:20])
    except (OSError, UnicodeDecodeError):
        return ""


def _manifesto(dest: Path) -> dict:
    """Conteúdo do manifesto oculto da pasta `dest` ({} quando não há manifesto legível)."""
    try:
        return json.loads((dest / MANIFESTO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _eh_portacao(alvo: Path) -> bool:
    """Reconhece uma portação do plugin: carimbo novo ou o cabeçalho de versões antigas (inclui ports feitos à mão)."""
    sk = alvo / "SKILL.md"
    if not sk.is_file():
        return False
    cab = _cabeca(sk)
    return "video-editores:port" in cab or "plugin [video-editores]" in cab


def _nossa(alvo: Path, nome: str, nossos: set) -> bool:
    """Portação nossa: SKILL.md com carimbo/cabeçalho do plugin, ou pasta que o manifesto lista (port interrompido
    — Ctrl-C ou disco cheio entre o mkdir e a escrita — que não pode virar 'alheia' só porque ficou sem SKILL.md)."""
    return _eh_portacao(alvo) if (alvo / "SKILL.md").is_file() else nome in nossos


def _versao_portada(sk: Path) -> str | None:
    # `(\d+(?:\.\d+)*)` e não `[0-9.]*`: o cabeçalho até a 0.13 era "… video-editores) 0.13.0." e o ponto final da
    # frase não pode entrar na versão; o `v` era opcional lá (e no carimbo sempre esteve)
    m = re.search(r"video-editores:port\s+v?(\d+(?:\.\d+)*)", _cabeca(sk))
    if not m:
        m = re.search(r"plugin \[video-editores\]\(\S*?\)\s+v?(\d+(?:\.\d+)*)", _cabeca(sk))
    return m.group(1) if m else None


def _estado_skills(dest: Path, agente: str, usuario: bool) -> str:
    """Estado da portação em `dest`: não instalado / em dia / DESATUALIZADO, lendo o manifesto ou os carimbos.

    O manifesto é a fonte primária (uma versão só); sem ele, os carimbos — todas as versões distintas, porque um
    re-port parcial deixaria parte das skills em versão velha e a primeira em ordem alfabética não representa nada.
    """
    if dest is None or not dest.is_dir():
        return "não instalado"
    vers = []
    man_vers = _manifesto(dest).get("versao")
    if man_vers:
        vers = [man_vers]
    else:
        vers = sorted({v for sk in sorted(dest.glob("*/SKILL.md")) if (v := _versao_portada(sk))})
    if not vers:
        return "não instalado"
    if set(vers) == {__version__}:
        return f"em dia ({__version__})"
    de_novo = f"video-editores integrar {agente}" + (" --usuario" if usuario else "")
    return f"DESATUALIZADO (port {', '.join(vers)} ≠ plugin {__version__}; rode `{de_novo}` de novo)"


# ---------------------------------------------------------------- skills
def _portar_skill(src: Path, dst: Path, raiz: Path, cabecalho: str | None = None):
    texto = src.read_text(encoding="utf-8")
    texto = texto.replace("${CLAUDE_PLUGIN_ROOT}", str(raiz)).replace("$CLAUDE_PLUGIN_ROOT", str(raiz))
    texto = texto.replace("/video-editores:", "/")  # nomes de slash do plugin não existem nos outros agentes
    m = re.match(r"(---\n.*?\n---\n)", texto, re.S)
    cab = (cabecalho or CABECALHO).format(repo=REPO, versao=__version__, raiz=raiz)
    corpo = texto[m.end():].lstrip("\n") if m else texto
    texto = ((m.group(1) if m else "") + CARIMBO + "\n" + cab + corpo)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "SKILL.md").write_text(texto, encoding="utf-8")
    for extra in src.parent.iterdir():  # arquivos de referência ao lado do SKILL.md (glossário, references/)
        if extra.name in ("SKILL.md", "scripts", "templates"):
            continue
        if extra.is_dir():
            shutil.copytree(extra, dst / extra.name, dirs_exist_ok=True)
        else:
            shutil.copy2(extra, dst / extra.name)


def exportar_skills(dest: Path, agente: str, escopo: str, remover=False, cabecalho=None) -> tuple:
    """Copia as skills do plugin para `dest/<nome>/SKILL.md` com guarda de procedência e manifesto oculto.

    Devolve (ok, atualizadas, puladas, falhas, nota): nomes gravados/removidos, ports substituídos, pastas alheias
    homônimas preservadas, (nome, erro) das que falharam e, na remoção de uma pasta compartilhada que outros
    agentes ainda dependem, a linha que substitui a do relatório.
    """
    raiz = raiz_recursos()
    ok, atualizadas, puladas, falhas = [], [], [], []
    man = _manifesto(dest)
    nossos = set(man.get("skills") or [])
    restantes = sorted(set(man.get("agentes") or []) - {agente})
    if remover and man.get("agentes") and restantes:
        # pasta compartilhada com outros agentes (Codex/OpenCode/ZCode dividem .agents/skills): só sai da lista
        try:
            (dest / MANIFESTO).write_text(json.dumps({**man, "agentes": restantes}, ensure_ascii=False, indent=2)
                                          + "\n", encoding="utf-8")
        except OSError as e:
            falhas.append((MANIFESTO, str(e)))
        return [], [], puladas, falhas, f"skills mantidas em {dest} — também usadas por {', '.join(restantes)}"
    for sk in sorted((raiz / "skills").glob("*/SKILL.md")):
        nome = sk.parent.name
        alvo = dest / nome
        if remover:
            if not alvo.is_dir():
                continue
            if not _nossa(alvo, nome, nossos):
                puladas.append(nome)  # não é nossa: não apaga
                continue
            try:
                shutil.rmtree(alvo)
                ok.append(nome)
            except OSError as e:
                falhas.append((nome, str(e)))
            continue
        if alvo.is_dir() and not _nossa(alvo, nome, nossos):
            puladas.append(nome)  # homônima alheia: não sobrescreve
            continue
        tinha = alvo.is_dir()
        try:
            _portar_skill(sk, alvo, raiz, cabecalho)
            ok.append(nome)
            if tinha:
                atualizadas.append(nome)
        except OSError as e:
            falhas.append((nome, str(e)))
    arq = dest / MANIFESTO
    if remover:
        if arq.is_file():
            try:
                arq.unlink()
            except OSError as e:
                falhas.append((MANIFESTO, str(e)))
    elif ok:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            arq.write_text(json.dumps({"plugin": NOME, "versao": __version__,
                                       "agentes": sorted(set(man.get("agentes") or []) | {agente}),
                                       "escopo": escopo, "raiz": str(raiz), "skills": ok},
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            falhas.append((MANIFESTO, str(e)))
    return ok, atualizadas, puladas, falhas, None


# ---------------------------------------------------------------- regras (AGENTS.md / steering)
def _nomes_skills() -> list:
    return sorted(p.parent.name for p in (raiz_recursos() / "skills").glob("*/SKILL.md"))


def texto_regras(raiz_midia: Path, chamada: str) -> str:
    return f"""## video-editores (edição de vídeo por IA local)

- Projeto de vídeo em `{raiz_midia}`: `originais/` é imutável, intermediários em `trabalho/<slug>/`, entregas em `saida/`, legendas em `legendas/`.
- Skills do plugin instaladas neste agente ({chamada}): {", ".join(_nomes_skills())}; `editar` é a entrada em linguagem natural.
- Execução: scripts chamados pelas skills, ou `video-editores <operação>` (`video-editores listar`), ou as ferramentas do servidor MCP `video-editores` (operações longas devolvem um id; acompanhe com `tarefa_status`).
- Pare para aprovação antes de descartar tomadas, queimar legenda, render longo e entrega (gates das skills). Texto na tela sempre em ASS, nunca `drawtext`. Áudio final a −14 LUFS.
- Modelos de IA e GPU por variáveis: `VE_DEVICE=cuda`, `VE_ENCODER=h264_nvenc`, `VE_ASR`/`VE_SEG`/`VE_TTS`. Documentação: {REPO} (docs/nucleo.md, docs/agentes.md).
"""


def gravar_bloco(arq: Path, bloco: str, remover=False, frontmatter: str = ""):
    """Insere/atualiza o bloco marcado em `arq`, preservando o resto do arquivo."""
    atual = arq.read_text(encoding="utf-8") if arq.is_file() else ""
    padrao = re.compile(re.escape(INI) + r".*?" + re.escape(FIM) + r"\n?", re.S)
    novo = padrao.sub("", atual)
    if not remover:
        novo = (novo.rstrip("\n") + "\n\n" if novo.strip() else frontmatter) + f"{INI}\n{bloco}{FIM}\n"
    if novo.strip() and novo.strip() != frontmatter.strip():
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(novo, encoding="utf-8")
    elif arq.is_file():
        arq.unlink()


# ---------------------------------------------------------------- MCP por agente
def _json_rw(arq: Path, muda, remover=False):
    if remover and not arq.is_file():
        return  # nada registrado: remover não pode deixar um config vazio num projeto que nunca foi integrado
    dados = json.loads(arq.read_text(encoding="utf-8")) if arq.is_file() and arq.read_text(encoding="utf-8").strip() else {}
    muda(dados)
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mcp_opencode(arq: Path, cmd: list, remover=False):
    def muda(d):
        d["mcp"] = d.get("mcp") or {}  # `"mcp": null` num config recém-criado pelo app não pode quebrar
        srv = d["mcp"].setdefault("servers", {})
        if remover:
            srv.pop(NOME, None)
        else:
            srv[NOME] = {"type": "local", "command": cmd, "timeout": {"startup": 120000, "execution": 7200000}}
    _json_rw(arq, muda, remover)


def mcp_kiro(arq: Path, cmd: list, remover=False):
    def muda(d):
        srv = d["mcpServers"] = d.get("mcpServers") or {}
        if remover:
            srv.pop(NOME, None)
        else:
            srv[NOME] = {"command": cmd[0], "args": cmd[1:], "disabled": False,
                         "autoApprove": ["listar_operacoes", "listar_modelos", "tarefa_status", "tarefas_listar", "versao", "ler_texto"]}
    _json_rw(arq, muda, remover)


def mcp_zcode(arq: Path, cmd: list, remover=False) -> str | None:
    """Config do ZCode (~/.zcode/cli/config.json): schema estrito — só as chaves conhecidas; o arquivo é gerenciado
    pelo próprio app, então este writer só adiciona/remove a chave do servidor e preserva o resto, e nunca o cria:
    arquivo ausente é ZCode não instalado, e um config parcial confundiria o app quando ele for instalado.
    Devolve um aviso quando nada foi gravado (vira linha do relatório)."""
    if not arq.is_file():
        return f"{arq} não existe (ZCode instalado?); nada {'a remover' if remover else 'gravado'} — o app cria este arquivo"

    def muda(d):
        d["mcp"] = d.get("mcp") or {}
        srv = d["mcp"].setdefault("servers", {})
        if remover:
            srv.pop(NOME, None)
        else:
            srv[NOME] = {"type": "stdio", "command": cmd[0], "args": cmd[1:], "enabled": True}
    _json_rw(arq, muda, remover)


def _toml_str(s):
    return json.dumps(s, ensure_ascii=False)  # strings TOML básicas aceitam a mesma escape do JSON


def mcp_codex(arq: Path, cmd: list, remover=False):
    """Edita só a tabela [mcp_servers.video-editores] do config.toml; o resto do arquivo fica como está."""
    atual = arq.read_text(encoding="utf-8") if arq.is_file() else ""
    padrao = re.compile(rf'^\[mcp_servers\."?{re.escape(NOME)}"?(\.[^\]]+)?\]\n(?:(?!^\[).*\n?)*', re.M)
    novo = padrao.sub("", atual).rstrip("\n")
    if not remover:
        bloco = (f'[mcp_servers."{NOME}"]\ncommand = {_toml_str(cmd[0])}\nargs = [{", ".join(_toml_str(a) for a in cmd[1:])}]\n'
                 f"startup_timeout_sec = 120\ntool_timeout_sec = 7200\n")
        novo = (novo + "\n\n" if novo else "") + bloco
    if novo.strip():
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(novo.rstrip("\n") + "\n", encoding="utf-8")
    elif arq.is_file():
        arq.unlink()


# ---------------------------------------------------------------- registro por agente
# Tudo que define um alvo de integração em um lugar: pares de caminho (projeto, usuario); None = não suportado.
REGISTRO = {
    "codex": {
        "chamada": "`$nome-da-skill` ou /skills",
        "cabecalho": None, "frontmatter": "", "nota_usuario": None,
        "skills": (".agents/skills", ".agents/skills"),
        "mcp": (".codex/config.toml", ".codex/config.toml"), "mcp_forma": mcp_codex,
        "regras": ("AGENTS.md", None),
    },
    "opencode": {
        "chamada": "`@nome-da-skill`",
        "cabecalho": None, "frontmatter": "", "nota_usuario": None,
        "skills": (".agents/skills", ".config/opencode/skills"),
        "mcp": ("opencode.json", ".config/opencode/opencode.json"), "mcp_forma": mcp_opencode,
        "regras": ("AGENTS.md", ".config/opencode/AGENTS.md"),
    },
    "kiro": {
        "chamada": "pela descrição ou `#`",
        "cabecalho": None, "frontmatter": "---\ninclusion: always\n---\n\n",
        "nota_usuario": "kiro: skills globais valem só na IDE; na CLI do Kiro use --projeto",
        "skills": (".kiro/skills", ".kiro/skills"),
        "mcp": (".kiro/settings/mcp.json", ".kiro/settings/mcp.json"), "mcp_forma": mcp_kiro,
        "regras": (f".kiro/steering/{NOME}.md", f".kiro/steering/{NOME}.md"),
    },
    "zcode": {
        "chamada": "pela descrição ou pedindo `use a skill <nome>`",
        "cabecalho": CABECALHO_ZCODE, "frontmatter": "", "nota_usuario": None,
        "dica_mcp": "o ZCode só registra MCP global; rode com --usuario",
        "skills": (".agents/skills", ".zcode/skills"),  # em projeto o ZCode lê a mesma pasta de Codex/OpenCode
        "mcp": (None, ".zcode/cli/config.json"), "mcp_forma": mcp_zcode,  # o ZCode só registra MCP global
        "regras": ("AGENTS.md", None),
    },
}
AGENTES = tuple(REGISTRO)


def _dica_mcp(agente: str) -> str:
    """Quando o alvo não tem registro de MCP no escopo pedido, o que o usuário deve fazer."""
    return REGISTRO[agente].get("dica_mcp") or "sem MCP neste escopo"


def alvos(agente: str, projeto: Path, usuario: bool) -> dict:
    reg = REGISTRO[agente]
    home = Path.home()

    def onde(par):
        c = par[1] if usuario else par[0]
        if c is None:
            return None
        return (home if usuario else projeto) / c

    return {"skills": onde(reg["skills"]), "mcp": onde(reg["mcp"]), "regras": onde(reg["regras"]),
            "cabecalho": reg["cabecalho"], "frontmatter": reg["frontmatter"], "chamada": reg["chamada"],
            "nota_usuario": reg["nota_usuario"]}


def integrar(agente, projeto, usuario=False, raiz_midia=None, skills=True, mcp=True, regras=True,
             remover=False, mostrar=False) -> tuple:
    """Executa a integração de um agente. Devolve (linhas_do_relatório, falhou)."""
    projeto = Path(projeto).resolve()
    raiz_midia = Path(raiz_midia or projeto).resolve()
    a = alvos(agente, projeto, usuario)
    escopo = "usuario" if usuario else "projeto"
    cmd = comando_mcp(raiz_midia)
    feito, falhou = [], False
    if _efemero(raiz_recursos()):
        feito.append("aviso: rodando de um ambiente efêmero do uvx; os caminhos dos scripts nas skills podem sumir. "
                     f"Instale com `uv tool install \"video-editores @ {REPO}\"` e rode de novo.")
    if mostrar:
        est = _estado_skills(a["skills"], agente, usuario)
        feito.append(f"{agente}: skills → {a['skills']}  ({est})")
        if a["mcp"]:
            feito.append(f"{agente}: MCP → {a['mcp']}  cmd: {' '.join(cmd)}")
        else:
            feito.append(f"{agente}: MCP → ({_dica_mcp(agente)})")
        feito.append(f"{agente}: regras → {a['regras'] or '(sem regras no escopo de usuário deste agente)'}")
        return feito, False
    # pastas/arquivos que outros agentes também gravam neste escopo: Codex, OpenCode e ZCode dividem .agents/skills
    # e AGENTS.md em projeto, então o cabeçalho e o bloco de regras têm que valer para os três
    compartilhada = any(o != agente and alvos(o, projeto, usuario)["skills"] == a["skills"] for o in AGENTES)
    if skills:
        ok, atualizadas, puladas, falhas, nota = exportar_skills(a["skills"], agente, escopo, remover,
                                                                 None if compartilhada else a["cabecalho"])
        if nota:
            feito.append(f"{agente}: {nota}")
        else:
            msg = f"{agente}: {'removidas' if remover else 'instaladas'} {len(ok)} skills em {a['skills']}"
            if atualizadas and not remover:
                msg += f" ({len(atualizadas)} atualizadas de port anterior)"
            feito.append(msg)
        if puladas:
            feito.append(f"{agente}: puladas por não serem do plugin (preservei): {', '.join(sorted(puladas))}")
        for nome, erro in falhas:
            feito.append(f"{agente}: falha em {nome}: {erro}")
        falhou = bool(falhas)
        if a["nota_usuario"]:
            feito.append(a["nota_usuario"])
    if mcp:
        if a["mcp"]:
            try:
                aviso = REGISTRO[agente]["mcp_forma"](a["mcp"], cmd, remover)
                feito.append(f"{agente}: {aviso}" if aviso else
                             f"{agente}: servidor MCP {'removido de' if remover else 'registrado em'} {a['mcp']}")
            except Exception as e:  # config ilegível (JSONC, corrompido) é falha do relatório, não traceback
                feito.append(f"{agente}: falha no MCP {a['mcp']}: {e}")
                falhou = True
        else:
            feito.append(f"{agente}: MCP ({_dica_mcp(agente)})")
    if regras and a["regras"]:
        try:
            man = _manifesto(a["skills"]) if a["skills"] else {}
            outros = sorted(o for o in AGENTES if o != agente and o in (man.get("agentes") or [])
                            and alvos(o, projeto, usuario)["regras"] == a["regras"])
            if remover and outros:
                # o arquivo de regras também é compartilhado: regrava o bloco para quem ainda depende dele
                chamada = "; ".join(f"{o} {alvos(o, projeto, usuario)['chamada']}" for o in outros)
                gravar_bloco(a["regras"], texto_regras(raiz_midia, chamada), False, frontmatter=a["frontmatter"])
                feito.append(f"{agente}: regras mantidas em {a['regras']} — também usadas por {', '.join(outros)}")
            else:
                chamada = a["chamada"]
                if not remover:
                    no_bloco = sorted({agente, *outros})
                    if len(no_bloco) > 1:  # o mesmo AGENTS.md serve a vários: as chamadas somadas
                        chamada = "; ".join(f"{o} {alvos(o, projeto, usuario)['chamada']}" for o in no_bloco)
                gravar_bloco(a["regras"], texto_regras(raiz_midia, chamada), remover, frontmatter=a["frontmatter"])
                feito.append(f"{agente}: regras {'removidas de' if remover else 'gravadas em'} {a['regras']}")
        except Exception as e:
            feito.append(f"{agente}: falha nas regras {a['regras']}: {e}")
            falhou = True
    return feito, falhou


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agente", choices=AGENTES + ("todos",))
    ap.add_argument("--projeto", default=os.getcwd(), help="pasta do projeto onde gravar (padrão: atual)")
    ap.add_argument("--usuario", action="store_true", help="grava na configuração global do agente em vez do projeto")
    ap.add_argument("--raiz-midia", default=None, help="raiz dos vídeos para o servidor MCP (padrão: --projeto)")
    ap.add_argument("--sem-skills", action="store_true"); ap.add_argument("--sem-mcp", action="store_true")
    ap.add_argument("--sem-regras", action="store_true")
    ap.add_argument("--remover", action="store_true", help="desfaz a integração (só o que é nosso)")
    ap.add_argument("--mostrar", action="store_true", help="mostra onde gravaria e o estado do que já está instalado")
    a = ap.parse_args(argv)
    falhou = False
    for ag in (AGENTES if a.agente == "todos" else (a.agente,)):
        linhas, f = integrar(ag, a.projeto, a.usuario, a.raiz_midia, not a.sem_skills, not a.sem_mcp,
                             not a.sem_regras, a.remover, a.mostrar)
        falhou = falhou or f
        for linha in linhas:
            print(linha)
    if not a.mostrar and not a.remover:
        print("Reinicie o agente para ele recarregar skills e servidores MCP.")
    return 1 if falhou else 0


if __name__ == "__main__":
    sys.exit(main())
