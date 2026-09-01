"""Print the project's readme path from pyproject.toml, or nothing if unset."""

# Standard library imports
import tomllib

with open("/app/pyproject.toml", "rb") as f:
  data = tomllib.load(f)

print(data.get("project", {}).get("readme", ""), end="")
