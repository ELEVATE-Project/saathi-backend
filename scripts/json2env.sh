set -euo pipefail
jq -r 'to_entries | map(
    "\(.key)=" + (
      if (.value | type) == "array" then (.value | map(tostring) | join(","))
      else (.value | tostring)
      end
    )
  ) | .[]'