import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routers import manuals, voc, commands, system, settings

app = FastAPI(title="Agent Platform Admin Console")

app.include_router(manuals.router)
app.include_router(voc.router)
app.include_router(commands.router)
app.include_router(system.router)
app.include_router(settings.router)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "../frontend")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
