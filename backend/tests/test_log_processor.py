from utils.log_processor import extract_error_context


def test_extract_error_context_finds_error():
    log = """
    Build started
    Running compilation
    ERROR: Compilation failed
    FAILED: Build step failed
    Build finished
    """

    result = extract_error_context(
        log,
        context_before=1,
        context_after=1
    )

    assert "ERROR: Compilation failed" in result
    assert "FAILED: Build step failed" in result