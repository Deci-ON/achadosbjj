from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}  # arquivos do Vite têm hash no nome
NO_CACHE = {"Cache-Control": "no-cache"}


def mount_frontend(app: FastAPI, directory: Path) -> bool:
    """Serve o front compilado (Vite) e devolve o index.html nas rotas do React Router.

    Deve ser chamada depois de registrar as rotas da API: as rotas anteriores têm prioridade sobre o
    coringa. Devolve False se a pasta não tiver index.html.
    """
    index = directory / "index.html"
    if not index.is_file():
        return False
    root = directory.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "rota da API não encontrada")  # nunca o index.html no lugar de um 404
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root in candidate.parents:  # não sai da pasta (../)
            return FileResponse(candidate, headers=IMMUTABLE if path.startswith("assets/") else NO_CACHE)
        return FileResponse(index, headers=NO_CACHE)

    return True
