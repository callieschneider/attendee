"""Task management tools."""
import logging

from .types import ToolDefinition, ToolSchema

log = logging.getLogger("agent.tools.tasks")


def _list_tasks(inp: dict, ctx: dict) -> dict:
    from agent.models import Task

    series_id = inp.get("series_id") or ctx.get("series_id")
    status_filter = inp.get("status")
    limit = min(int(inp.get("limit", 20)), 50)

    qs = Task.objects.all()
    if series_id:
        qs = qs.filter(series_id=series_id)
    if status_filter:
        qs = qs.filter(status=status_filter)
    else:
        qs = qs.exclude(status__in=["done", "cancelled"])

    tasks = []
    for t in qs.order_by("priority", "due_date")[:limit]:
        tasks.append({
            "id": str(t.id),
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "owner": t.owner,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "description": t.description[:200],
        })
    return {"tasks": tasks, "count": len(tasks)}


def _create_task(inp: dict, ctx: dict) -> dict:
    from agent.models import Task, MeetingSeries

    series_id = inp.get("series_id") or ctx.get("series_id")
    if not series_id:
        return {"error": "series_id required"}
    try:
        series = MeetingSeries.objects.get(id=series_id)
    except MeetingSeries.DoesNotExist:
        return {"error": f"series {series_id} not found"}

    task = Task.objects.create(
        series=series,
        title=inp.get("title", "")[:512],
        description=inp.get("description", ""),
        priority=inp.get("priority", "medium"),
        owner=inp.get("owner", ""),
        status="todo",
    )
    if inp.get("due_date"):
        from datetime import date
        try:
            task.due_date = date.fromisoformat(inp["due_date"])
            task.save()
        except ValueError:
            pass

    return {"created": True, "task_id": str(task.id), "title": task.title}


def _update_task_status(inp: dict, ctx: dict) -> dict:
    from agent.models import Task

    task_id = inp.get("task_id")
    new_status = inp.get("status")
    if not task_id or not new_status:
        return {"error": "task_id and status required"}

    valid_statuses = ["backlog", "todo", "in_progress", "in_review", "done", "cancelled"]
    if new_status not in valid_statuses:
        return {"error": f"status must be one of {valid_statuses}"}

    try:
        task = Task.objects.get(id=task_id)
    except Task.DoesNotExist:
        return {"error": f"task {task_id} not found"}

    task.status = new_status
    task.save(update_fields=["status", "updated_at"])
    return {"updated": True, "task_id": task_id, "new_status": new_status}


TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="list_tasks",
        description="List tasks for a meeting series. By default returns only open tasks (not done/cancelled).",
        input_schema=ToolSchema(
            type="object",
            properties={
                "series_id": {"type": "string", "description": "Filter by series UUID (optional)"},
                "status": {
                    "type": "string",
                    "description": "Filter by specific status: backlog, todo, in_progress, in_review, done, cancelled",
                    "enum": ["backlog", "todo", "in_progress", "in_review", "done", "cancelled"],
                },
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        ),
        handler=_list_tasks,
    ),
    ToolDefinition(
        name="create_task",
        description="Create a new task in a meeting series.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "series_id": {"type": "string", "description": "UUID of the MeetingSeries"},
                "title": {"type": "string", "description": "Task title"},
                "description": {"type": "string", "description": "Optional task details"},
                "priority": {
                    "type": "string",
                    "description": "Task priority",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "owner": {"type": "string", "description": "Person responsible (name or email)"},
                "due_date": {"type": "string", "description": "Due date in YYYY-MM-DD format"},
            },
            required=["series_id", "title"],
        ),
        handler=_create_task,
    ),
    ToolDefinition(
        name="update_task_status",
        description="Update the status of a task.",
        input_schema=ToolSchema(
            type="object",
            properties={
                "task_id": {"type": "string", "description": "UUID of the Task"},
                "status": {
                    "type": "string",
                    "description": "New status",
                    "enum": ["backlog", "todo", "in_progress", "in_review", "done", "cancelled"],
                },
            },
            required=["task_id", "status"],
        ),
        handler=_update_task_status,
    ),
]
