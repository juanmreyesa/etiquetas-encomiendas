"""Apunta la base a un archivo temporal ANTES de importar la app/db."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="etq_test_")
os.environ.setdefault("ETIQUETAS_DB", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
