from .models import AuditLog

def log_activity(user, activity, details=None):
    AuditLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        activity=activity,
        details=details or {},
    )
