from tc_rag.interfaces.chunk_stats import chunk_stats
from tc_rag.interfaces.kb.commands import app as kb_app
from tc_rag.interfaces.vision_check import vision
from tc_rag.interfaces.workflow import app

app.command()(vision)
app.command()(chunk_stats)
app.add_typer(kb_app, name="kb")

if __name__ == "__main__":
    app()
