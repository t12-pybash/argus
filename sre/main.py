import typer
from sre.commands.status import status as _status
from sre.commands.alert import alert as _alert
from sre.commands.ask import ask as _ask

app = typer.Typer(help="Homelab SRE CLI", no_args_is_help=True)


@app.command()
def status():
    """Show cluster health: nodes, alerts, high-restart pods."""
    _status()


@app.command()
def alert():
    """List active Alertmanager alerts."""
    _alert()


@app.command()
def ask(question: str = typer.Argument(..., help="Question to ask about the cluster")):
    """Ask the LLM a question about the cluster using live Prometheus context."""
    _ask(question)
