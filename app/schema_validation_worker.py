"""Standalone child: no service settings, credentials or application imports."""

import json
import sys

from jsonschema import validators
from jsonschema.exceptions import best_match
from referencing import Registry
from referencing.exceptions import NoSuchResource


def reject_reference(uri):
    raise NoSuchResource(ref=uri)


def validate(schema, instance):
    validator = validators.validator_for(schema)
    validator.check_schema(schema)
    error = best_match(
        validator(schema, registry=Registry(retrieve=reject_reference)).iter_errors(instance)
    )
    return [] if error is None else [{"path": "$", "message": error.message}]


def main():
    try:
        data = sys.stdin.buffer.read(3 * 1024 * 1024 + 1)
        if len(data) > 3 * 1024 * 1024:
            raise ValueError("request too large")
        request = json.loads(data)
        errors = validate(request["schema"], request["instance"])
        errors = [
            {key: detail[key].encode()[:256].decode(errors="ignore") for key in ("path", "message")}
            for detail in errors[:12]
        ]
        response = json.dumps({"errors": errors}, ensure_ascii=False).encode()
        while len(response) > 8192 and len(errors) > 1:
            errors.pop()
            response = json.dumps({"errors": errors}, ensure_ascii=False).encode()
    except Exception:
        response = b'{"failure":"validation"}'
    sys.stdout.buffer.write(response)


if __name__ == "__main__":
    main()
