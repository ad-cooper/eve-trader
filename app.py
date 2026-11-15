#!/usr/bin/env python3
"""
EVE Trading Desk - FastAPI Application Entrypoint
=================================================
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from routers import api, auth
from workflows.scheduler import start_background_scheduler, stop_background_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_background_scheduler()
    yield
    stop_background_scheduler()


app = FastAPI(title="EVE Trading Desk", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(auth.router)
app.include_router(api.router)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
