from tc_rag.interfaces.vision_check import vision
from tc_rag.interfaces.workflow import app

app.command()(vision)

if __name__ == "__main__":
    app()
