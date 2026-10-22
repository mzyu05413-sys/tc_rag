"""Command-line entry point."""

import typer

app = typer.Typer(help="tc_rag: traceable requirement-driven test design.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Build test designs from requirement documents (P0 scaffold)."""


if __name__ == "__main__":
    app()
