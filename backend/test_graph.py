from graph import analyze_log


result = analyze_log("""
Starting Jenkins build

ERROR: Bazel command failed
ERROR: invalid command-line option

FAILED: unit tests

Build FAILURE
""")


print("\nFINAL STATE:")
print(result)