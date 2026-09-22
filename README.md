# Jenkins AI Analyzer

An AI-powered Jenkins build log analyzer that automatically identifies build failures, extracts the relevant evidence, determines the likely root cause, and recommends an actionable fix.

The application uses **FastAPI**, **LangGraph**, and **Groq LLMs**, with a lightweight web frontend and Docker Compose for containerized execution.

---

## Features

- Upload Jenkins build logs through a web interface
- Extract relevant error and failure contexts from large logs
- Detect the primary build error
- Identify the affected component
- Perform AI-based root cause analysis
- Extract supporting evidence from the original log
- Deterministically validate evidence
- Validate the generated analysis
- Retry root-cause analysis when validation fails
- Provide recommended fixes
- Provide confidence level
- FastAPI REST API
- LangGraph multi-agent workflow
- Dockerized backend
- Docker Compose development environment
- Automated Python tests
- GitHub Actions CI

---

## Architecture

```text
                    Jenkins Build Log
                           |
                           v
                    Web Frontend
                           |
                           v
                    FastAPI Backend
                           |
                           v
                      LangGraph
                           |
             +-------------+-------------+
             |             |             |
             v             v             v
          Parser      Error Detector  Root Cause
             |             |             |
             +-------------+-------------+
                           |
                           v
                       Validator
                           |
                    +------+------+
                    |             |
                  Valid         Invalid
                    |             |
                    v             v
                   END       Retry Root Cause