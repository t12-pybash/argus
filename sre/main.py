import typer
from sre.commands.status import status as _status
from sre.commands.alert import alert as _alert
from sre.commands.ask import ask as _ask
from sre.commands.runbook import runbook as _runbook
from sre.commands.diagnose import diagnose as _diagnose
from sre.commands import incident as _inc

app = typer.Typer(help="Homelab SRE CLI", no_args_is_help=True)
incident_app = typer.Typer(help="Incident management", no_args_is_help=True)
app.add_typer(incident_app, name="incident")


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


@app.command()
def runbook(
    query: str = typer.Argument(..., help="What to search for in the vault"),
    ask: bool = typer.Option(False, "--ask", "-a", help="Synthesise an answer via LLM"),
    generate: bool = typer.Option(False, "--generate", "-g", help="Generate and save a structured runbook to vault"),
):
    """Search vault runbooks semantically. Use --ask to synthesise, --generate to create and save."""
    _runbook(query, ask=ask, generate=generate)


@app.command()
def diagnose(
    pod: str = typer.Argument(..., help="Pod reference as namespace/pod-name"),
    save: bool = typer.Option(False, "--save", "-s", help="Save diagnosis as runbook to vault"),
):
    """Diagnose a crashing pod: fetches logs, events, describe and gets LLM root cause analysis."""
    _diagnose(pod, save=save)


@incident_app.command("start")
def incident_start(title: str = typer.Argument(..., help="Brief description of the incident")):
    """Start a new incident and create a vault note with current cluster state."""
    _inc.start(title)


@incident_app.command("note")
def incident_note(message: str = typer.Argument(..., help="Observation or action taken")):
    """Append a timestamped note to the active incident."""
    _inc.note(message)


@incident_app.command("close")
def incident_close(summary: str = typer.Argument(..., help="Resolution summary")):
    """Close the active incident with a resolution summary."""
    _inc.close(summary)


@incident_app.command("show")
def incident_show():
    """Show the currently active incident."""
    _inc.show()
