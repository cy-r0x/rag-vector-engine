import os

os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-characters-long"
# The API's background status consumer would otherwise attach to the real document.events queue.
os.environ["CONSUME_DOCUMENT_EVENTS"] = "false"
