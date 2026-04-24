"""Work-type templates for the structured task form (Epic #32, task #44).

Templates are plain YAML files that map onto the ``TaskRefine`` schema
(``hub.models.TaskRefine``). They are designed to be a starting point —
fill in the placeholder strings, drop the fields you don't need, then
feed the file to::

    oc-hub refine <task_id> --from-file path/to/file.yaml

Templates are shipped as package data so ``importlib.resources`` works
in editable installs, wheels, and zipapps.
"""
