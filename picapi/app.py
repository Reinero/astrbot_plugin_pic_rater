from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api.routes import router
from config import GALLERY_DIR, STATIC_PREFIX
from infra.migrations import migrate

app = FastAPI(title="Picture API with Ratings", version="3.0.0")
app.mount(STATIC_PREFIX, StaticFiles(directory=str(GALLERY_DIR), html=False), name="static")
app.include_router(router)

@app.on_event("startup")
def startup():
    migrate()

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": f"http_{exc.status_code}", "message": "request failed", "detail": exc.detail},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "internal server error", "detail": str(exc)},
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


