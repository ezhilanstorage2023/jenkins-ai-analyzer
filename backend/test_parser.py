from utils.log_processor import extract_error_context


log = """
Starting Jenkins build

Checking configuration
Using Bazel 7.0

Compiling application

ERROR: invalid command-line option
ERROR: unknown argument --example

Running tests

FAILED: unit tests

Build FAILURE
"""


result = extract_error_context(log)

print(result)