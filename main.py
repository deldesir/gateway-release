import uvicorn
from app.api.app import create_app


def main() -> None:
    """
    Start the FastAPI application.
    """
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
