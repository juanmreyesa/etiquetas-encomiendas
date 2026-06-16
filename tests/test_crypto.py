import crypto


def test_roundtrip():
    tok = crypto.enc("s3cr3t")
    assert tok != "s3cr3t"           # cifrado (no texto plano)
    assert crypto.dec(tok) == "s3cr3t"


def test_dec_tolera_texto_plano_y_vacios():
    assert crypto.dec("texto-plano-viejo") == "texto-plano-viejo"  # legado
    assert crypto.dec("") == ""
    assert crypto.dec(None) == ""
    assert crypto.enc("") == ""
    assert crypto.enc(None) == ""
