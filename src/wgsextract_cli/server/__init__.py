"""Server mode (``wgsextract serve``): the typed API (``wgsextract_cli.api``)
over local HTTP/JSON and MCP.

Both front ends serve the same operations, found in the API's group modules
(``operations``), and run them as jobs (``jobs``): each in its own worker
process (``worker``) that calls the API function, so the server adds no
per-command logic. See docs/server.md.
"""
