####
## Streamlit App for Agentic Data Quality Triage
## Author: Mario Caesar // hello@caesarmar.io // https://caesarmar.io/
####

from __future__ import annotations

import streamlit as st

from pipelines.common.logging import logger


def render_header() -> None:
    """
    Render the Streamlit page header.

    Returns:
        None.
    """
    st.title("Agentic DQ Triage Platform")
    st.caption("Local demo UI for alerts, evidence, and agentic triage reports.")


def main() -> None:
    """
    Configure and render the Streamlit application.

    Returns:
        None.
    """
    logger.info("Rendering Streamlit app")

    st.set_page_config(page_title="Agentic DQ Triage Platform", layout="wide")
    render_header()

    # Keep the first UI intentionally small until ClickHouse alerts are wired in.
    st.info("Streamlit is running. Alert browsing and triage actions will be added after the core pipeline is ready.")


if __name__ == "__main__":
    main()
