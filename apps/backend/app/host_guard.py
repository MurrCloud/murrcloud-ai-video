from app.admission import evaluate


def require_admission(settings) -> bool:
    return evaluate(str(settings.host_signal_path))[0]
