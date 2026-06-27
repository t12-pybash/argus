import typer
from argus.commands.status import status as _status
from argus.commands.alert import alert as _alert
from argus.commands.ask import ask as _ask
from argus.commands.runbook import runbook as _runbook
from argus.commands.diagnose import diagnose as _diagnose
from argus.commands import incident as _inc
from argus.commands import agent as _agent
from argus.commands import model as _model

app = typer.Typer(help="Argus — autonomous SRE intelligence for sovereign infrastructure", no_args_is_help=True)
incident_app = typer.Typer(help="Incident management", no_args_is_help=True)
agent_app = typer.Typer(help="Autonomous SRE agent", no_args_is_help=True)
model_app = typer.Typer(help="Manage KServe InferenceServices", no_args_is_help=True)
app.add_typer(incident_app, name="incident")
app.add_typer(agent_app, name="agent")
app.add_typer(model_app, name="model")


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


@agent_app.command("start")
def agent_start(
    auto: bool = typer.Option(False, "--auto", "-a", help="Auto-execute safe remediation without approval"),
    interval: int = typer.Option(30, "--interval", "-i", help="Poll interval in seconds"),
    namespaces: str = typer.Option("", "--namespaces", "-n", help="Comma-separated namespaces to watch (default: all)"),
):
    """Start the autonomous SRE agent daemon."""
    _agent.start(auto=auto, interval=interval, namespaces=namespaces)


@agent_app.command("approve")
def agent_approve():
    """Approve the pending remediation action."""
    _agent.approve()


@agent_app.command("reject")
def agent_reject():
    """Reject the pending remediation action."""
    _agent.reject()


@agent_app.command("skip")
def agent_skip():
    """Skip the pending remediation action (alias for reject)."""
    _agent.reject()


@agent_app.command("status")
def agent_status():
    """Show agent status and any pending action."""
    _agent.status()


@model_app.command("status")
def model_status():
    """Show InferenceService health and available models."""
    _model.status()


@model_app.command("rollout")
def model_rollout(
    image: str = typer.Argument(..., help="New container image (e.g. ollama/ollama:latest)"),
    name: str = typer.Option("ollama", "--name", "-n", help="InferenceService name"),
    namespace: str = typer.Option("ai-platform", "--namespace", help="Namespace"),
):
    """Update the container image of an InferenceService."""
    _model.rollout(name, image, namespace)


@model_app.command("rollback")
def model_rollback(
    name: str = typer.Option("ollama", "--name", "-n", help="InferenceService name"),
    namespace: str = typer.Option("ai-platform", "--namespace", help="Namespace"),
):
    """Rollback an InferenceService to the previous image."""
    _model.rollback(name, namespace)


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
