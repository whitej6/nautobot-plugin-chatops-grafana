"""Worker function for /net commands in Slack."""
import tempfile
import argparse
import os
from datetime import datetime
from typing import NoReturn
from isodate import ISO8601Error, parse_duration
from jinja2 import Template
from django_rq import job
from django.core.exceptions import FieldError
from pydantic.error_wrappers import ValidationError  # pylint: disable=no-name-in-module
from nautobot.dcim import models
from nautobot.utilities.querysets import RestrictedQuerySet
from nautobot_chatops.workers import handle_subcommands, add_subcommand
from .grafana import SLASH_COMMAND, LOGGER, GRAFANA_LOGO_PATH, GRAFANA_LOGO_ALT, REQUEST_TIMEOUT_SEC, handler
from .exceptions import DefaultArgsError, PanelError, MultipleOptionsError


def grafana_logo(dispatcher):
    """Construct an image_element containing the locally hosted Grafana logo."""
    return dispatcher.image_element(dispatcher.static_url(GRAFANA_LOGO_PATH), alt_text=GRAFANA_LOGO_ALT)


@job("default")
def grafana(subcommand, **kwargs):
    """Pull Panels from Grafana."""
    initialize_subcommands()
    handler.current_subcommand = subcommand
    return handle_subcommands(SLASH_COMMAND, subcommand, **kwargs)


def initialize_subcommands():
    """Based on the panels configuration yaml provided build chat subcommands."""
    raw_panels = handler.panels
    default_params = [
        f"width={handler.width}",
        f"height={handler.height}",
        f"theme={handler.theme}",
        f"timespan={handler.timespan}",
        f"timezone={handler.timezone}",
    ]
    for dashboard in raw_panels["dashboards"]:
        for panel in dashboard["panels"]:
            panel_variables = []
            # Build parameters list from dynamic variables in panels
            for variable in panel.get("variables", []):
                if variable.get("includeincmd", True):
                    panel_variables.append(variable["name"])
            # The subcommand name with be get-{command_name}
            add_subcommand(
                command_name=SLASH_COMMAND,
                command_func=grafana,
                subcommand_name=f"get-{panel['command_name']}",
                subcommand_spec={
                    "worker": chat_get_panel,
                    "params": panel_variables + default_params,
                    "doc": panel["friendly_name"],
                },
            )


def chat_get_panel(dispatcher, *args) -> bool:
    """High level function to handle the panel request.

    Args:
        dispatcher (nautobot_chatops.dispatchers.Dispatcher): Abstracted dispatcher class for chat-ops.

    Returns:
        bool: ChatOps response pass or fail.
    """
    panel, parsed_args, dashboard_slug = chat_parse_args(dispatcher, *args)
    if not parsed_args:
        return False

    try:
        # Validate nautobot Args and get any missing parameters
        chat_validate_nautobot_args(
            dispatcher=dispatcher,
            panel=panel,
            parsed_args=parsed_args,
            action_id=f"grafana {handler.current_subcommand} {' '.join(args)}",
        )

    except PanelError as exc:
        dispatcher.send_error(f"Sorry, {dispatcher.user_mention()} there was an error with the panel definition, {exc}")
        return False

    except MultipleOptionsError:
        return False

    try:
        # Validate the default arguments to make sure the conform to their defined pydantic type.
        chat_validate_default_args(parsed_args=parsed_args)

    except DefaultArgsError as exc:
        dispatcher.send_error(exc)
        return False

    return chat_return_panel(dispatcher, panel, parsed_args, dashboard_slug)


def chat_parse_args(dispatcher, *args):
    """Parse the arguments from the user via chat using argparser.

    Args:
        dispatcher (nautobot_chatops.dispatchers.Dispatcher): Abstracted dispatcher class for chat-ops.

    Returns:
        panel: dict the panel dict from the configuration file
        parsed_args: dict of the arguments from the user's raw input
        dashboard_slug: str the dashboard slug
    """
    raw_panels = handler.panels
    dashboard_slug = None
    panel = None

    # Find the panel config matching the current subcommand
    for dashboard in raw_panels["dashboards"]:
        panel = next((i for i in dashboard["panels"] if f"get-{i['command_name']}" == handler.current_subcommand), None)
        if panel:
            dashboard_slug = dashboard["dashboard_slug"]
            break

    if not panel:
        dispatcher.send_error(f"Command {handler.current_subcommand} Not Found!")
        return False

    # Append on the flag command to conform to argparse parsing methods.
    fixed_args = []
    for arg in args:
        if arg.startswith(handler.default_params):
            fixed_args.append(f"--{arg}")
        else:
            fixed_args.append(arg)

    # Collect the arguments sent by the user parse them matching the panel config
    parser = argparse.ArgumentParser(description="Handles command arguments")
    predefined_args = {}
    for variable in panel.get("variables", []):
        if variable.get("includeincmd", True):
            parser.add_argument(f"{variable['name']}", default=variable.get("response", ""), nargs="?")
        else:
            # The variable from the config wasn't included in the users response (hidden) so
            # ass the default response if provided in the config
            predefined_args[variable["name"]] = variable.get("response", "")

    parser.add_argument("--width", default=handler.width, nargs="?")
    parser.add_argument("--height", default=handler.height, nargs="?")
    parser.add_argument("--theme", default=handler.theme, nargs="?")
    parser.add_argument("--timespan", default=handler.timespan, nargs="?")
    parser.add_argument("--timezone", default=handler.timezone, nargs="?")
    args_namespace = parser.parse_args(fixed_args)
    parsed_args = {**vars(args_namespace), **predefined_args}
    return panel, parsed_args, dashboard_slug


def chat_return_panel(dispatcher, panel, parsed_args, dashboard_slug) -> bool:
    """After everything passes the tests decorate the response and return the panel to the user.

    Args:
        dispatcher (nautobot_chatops.dispatchers.Dispatcher): Abstracted dispatcher class for chat-ops.
        panel ([type]): [description]
        parsed_args ([type]): [description]
        dashboard_slug ([type]): [description]

    Returns:
        bool: ChatOps response pass or fail.
    """
    dispatcher.send_markdown(
        f"Standby {dispatcher.user_mention()}, I'm getting that result.\n"
        f"Please be patient as this can take up to {REQUEST_TIMEOUT_SEC} seconds.",
        ephemeral=True,
    )
    dispatcher.send_busy_indicator()

    raw_png = handler.get_png(dashboard_slug, panel)
    if not raw_png:
        dispatcher.send_error("An error occurred while accessing Grafana")
        return False

    chat_header_args = []
    for variable in panel.get("variables", []):
        if variable.get("includeincmd", True):
            chat_header_args.append(
                (variable.get("friendly_name", variable["name"]), str(parsed_args[variable["name"]]))
            )
    dispatcher.send_blocks(
        dispatcher.command_response_header(
            SLASH_COMMAND,
            handler.current_subcommand,
            chat_header_args[:5],
            panel["friendly_name"],
            grafana_logo(dispatcher),
        )
    )

    with tempfile.TemporaryDirectory() as tempdir:
        # Note: Microsoft Teams will silently fail if we have ":" in our filename.
        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d-%H-%M-%S")

        # If a timespan is specified, set the filename of the image to be the correct timespan displayed in the
        # Grafana image.
        if parsed_args.get("timespan"):
            timedelta = parse_duration(parsed_args.get("timespan")).totimedelta(start=now)
            from_ts = (now - timedelta).strftime("%Y-%m-%d-%H-%M-%S")
            time_str = f"{from_ts}-to-{time_str}"

        img_path = os.path.join(tempdir, f"{handler.current_subcommand}_{time_str}.png")
        with open(img_path, "wb") as img_file:
            img_file.write(raw_png)
        dispatcher.send_image(img_path)
    return True


def chat_validate_nautobot_args(dispatcher, panel, parsed_args, action_id) -> NoReturn:
    """Parse through args and validate them against the definition with the panel.

    Args:
        dispatcher ([type]): [description]
        panel ([type]): [description]
        parsed_args ([type]): [description]
        action_id ([type]): [description]

    Raises:
        PanelError: An issue fetching objects based on panel variables.
        MultipleOptionsError: Objects retrieved from Nautobot is not specific enough to process, return choices to user.

    Returns:
        NoReturn
    """
    validated_variables = {}

    for variable in panel.get("variables", []):
        if not variable.get("query", False):
            LOGGER.debug("Validated variable %s with input %s", variable["name"], parsed_args[variable["name"]])
            validated_variables[variable["name"]] = parsed_args[variable["name"]]
        else:
            LOGGER.debug("Validating variable %s with input %s", variable["name"], parsed_args[variable["name"]])
            # A nautobot Query is defined so first lets get all of those objects
            objects = get_nautobot_objects(variable=variable)

            # Now lets validate the object and prompt the user for a correct object
            _filter = variable.get("filter", {})

            # If the user specified a filter in the chat command:
            # i.e. /grafana get-<name> 'site', and 'site' exist as the variable name,
            # we will add it to the filter.
            if parsed_args.get(variable["name"]):
                _filter[variable["modelattr"]] = parsed_args[variable["name"]]

            # Parse Jinja in filter
            for filter_key in _filter.keys():
                template = Template(_filter[filter_key])
                _filter[filter_key] = template.render(validated_variables)

            try:
                filtered_objects = objects.filter(**_filter)
            except FieldError:
                LOGGER.error("Unable to filter %s by %s", variable["query"], _filter)
                raise PanelError(f"I was unable to filter {variable['query']} by {_filter}") from None

            # filtered_objects should be a single record by this point. If not, we cannot process further,
            # we need to prompt the user for the options to filter further.
            if filtered_objects.count() != 1:
                if filtered_objects.count() > 1:
                    choices = [
                        (f"{filtered_object.name}", getattr(filtered_object, variable["modelattr"]))
                        for filtered_object in filtered_objects
                    ]
                else:
                    choices = [(f"{obj.name}", getattr(obj, variable["modelattr"])) for obj in objects]
                helper_text = (
                    f"{panel['friendly_name']} Requires {variable['friendly_name']}"
                    if variable.get("friendly_name", False)
                    else panel["friendly_name"]
                )
                parsed_args[variable["name"]] = dispatcher.prompt_from_menu(action_id, helper_text, choices)
                raise MultipleOptionsError

            # Add the validated device to the dict so templates can use it later
            LOGGER.debug("Validated variable %s with input %s", variable["name"], parsed_args[variable["name"]])
            validated_variables[variable["name"]] = filtered_objects[0].__dict__

        # Now we now we have a valid device lets parse the value template for this variable
        template = Template(variable.get("value", str(validated_variables[variable["name"]])))
        variable["value"] = template.render(validated_variables)


def get_nautobot_objects(variable: dict) -> RestrictedQuerySet:
    """get_nautobot_objects fetches objects from the Nautobot ORM based on user-defined query params.

    Args:
        variable (dict): Variables defined in panels.yml for a specific dashboard.

    Raises:
        PanelError: An issue fetching objects based on panel variables.

    Returns:
        RestrictedQuerySet: Objects returned from the Nautobot ORM.
    """
    try:
        # Example, if a 'query' defined in panels.yml is set to 'Site', we would pull all sites
        # using 'Site.objects.all()'
        objects = getattr(models, variable["query"]).objects.all()
    except AttributeError as exc:
        LOGGER.error("Unable to find class %s in dcim.models: %s", variable["query"], exc)
        raise PanelError(f"I was unable to find class {variable['query']} in dcim.models") from None

    if not variable.get("modelattr", False):
        raise PanelError("When specifying a query, a modelattr is also required")
    if objects.count() < 1:
        raise PanelError(f"{variable['query']} returned {objects.count()} items in the dcim.model.")

    return objects


def chat_validate_default_args(parsed_args: dict) -> NoReturn:
    """chat_validate_default_args will run pydantic validation checks against the default arguments.

    Args:
        parsed_args (dict): Combination of default and panel specified arguments, parsed into a dict.

    Raises:
        DefaultArgsError: An error validating the default arguments against their defined pydantic types.

    Returns:
        NoReturn
    """
    # Validate and set the default arguments
    for default_arg in handler.default_params:
        try:
            setattr(handler, default_arg, parsed_args[default_arg])
        except (ValidationError, ISO8601Error) as exc:
            raise DefaultArgsError(parsed_args[default_arg], exc) from None
