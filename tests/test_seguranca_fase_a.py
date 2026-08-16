"""Testes das correções de segurança da Fase A."""
import os, sys, json
sys.path.insert(0, "/Users/jonathassilva/Documents/Claude/Digitalizei Transcrição")
import pytest
from social import intercept
import subscriptions as subs


# ── code malicioso não passa do funil de entrada ──────────────────────────────
@pytest.mark.parametrize("bad", [
    "x' + require('child_process').execSync('id') + '",   # injeção no Node
    "abc'; alert(1); //",                                  # quebra de onclick
    "javascript:alert(1)",
    "../../etc/passwd",
    "a" * 100,
    "", None,
])
def test_code_malicioso_vira_vazio(bad):
    assert intercept._safe_code(bad) == ""

@pytest.mark.parametrize("ok", ["Dau1GKXOViH", "7234567890123456789", "abc-DEF_123"])
def test_code_legitimo_passa(ok):
    assert intercept._safe_code(ok) == ok

def test_row_sanitiza_code():
    r = intercept._row(code="x'+evil()+'", url="https://x/y")
    assert r["code"] == ""

# ── URL vai para o script Node como literal JSON (não interpolada crua) ───────
def test_url_no_script_e_json():
    assert "const TARGET_URL = __TARGET_URL__" in intercept.NODE_TEMPLATE
    assert "'__TARGET_URL__'" not in intercept.NODE_TEMPLATE
    assert "'__TARGET_URL__'" not in intercept.RESOLVE_TEMPLATE

def test_json_dumps_neutraliza_aspas():
    evil = "https://x/'+require('child_process').execSync('id')+'"
    encoded = json.dumps(evil)
    script = intercept.NODE_TEMPLATE.replace("__TARGET_URL__", encoded)
    # a aspa simples do payload não pode aparecer "solta" fechando um literal
    linha = [l for l in script.splitlines() if l.startswith("const TARGET_URL")][0]
    assert linha.startswith('const TARGET_URL = "')   # virou string JSON com aspas duplas
    assert "\\'" in encoded or "'" in encoded          # a aspa está DENTRO do literal

# ── scheme validado nas assinaturas ──────────────────────────────────────────
def test_clean_target_rejeita_scheme_estranho():
    with pytest.raises(ValueError):
        subs._clean_target("youtube", "ftp://youtube.com/@canal")

def test_clean_target_aceita_https():
    assert subs._clean_target("youtube", "https://youtube.com/@canal")
