"""This module is intended to handle grafana requests generically perhaps outside of nautobot."""
import datetime
import logging
import urllib.parse
from typing import Union, Tuple
import requests
import isodate
import yaml

from django.conf import settings
from jsonschema import ValidationError
from pydantic import BaseModel, FilePath  # pylint: disable=no-name-in-module
from requests.exceptions import RequestException
from typing_extensions import Literal
from nautobot_plugin_chatops_grafana.validator import validate

LOGGER = logging.getLogger("nautobot.plugin.grafana")
PLUGIN_SETTINGS = settings.PLUGINS_CONFIG["nautobot_plugin_chatops_grafana"]

SLASH_COMMAND = "grafana"
GRAFANA_LOGO_PATH = "grafana/grafana_icon.png"
GRAFANA_LOGO_ALT = "Grafana Logo"
REQUEST_TIMEOUT_SEC = 60


class GrafanaConfigSettings(BaseModel):  # pylint: disable=too-few-public-methods
    """Model for config parameters to validate config schema."""

    grafana_url: str
    grafana_api_key: str
    default_width: int
    default_height: int
    default_theme: Literal["light", "dark"]
    default_timespan: datetime.timedelta
    grafana_org_id: int
    default_tz: str
    config_file: FilePath


class GrafanaHandler:
    """Handle Building Grafana Requests."""

    config: GrafanaConfigSettings = None
    panels = None
    current_subcommand = ""
    now = datetime.datetime.utcnow()
    default_params = ("width", "height", "theme", "timespan", "timezone")

    def __init__(self, config: dict) -> None:
        """Initialize the class."""
        self.config = GrafanaConfigSettings(**config)
        self.load_panels()

    def load_panels(self):
        """This method loads the yaml configuration file, and validates the schema."""
        # Validate the YAML configuration files.
        schema_errors = validate()
        if schema_errors:
            raise ValidationError(",".join(schema_errors))

        with open(self.config.config_file) as config_file:
            self.panels = yaml.safe_load(config_file)

    @property
    def width(self):
        """Simple Get Width."""
        return self.config.default_width

    @property
    def height(self):
        """Simple Get Height."""
        return self.config.default_height

    @property
    def theme(self):
        """Simple Get Theme."""
        return self.config.default_theme

    @property
    def timespan(self):
        """Simple Get Timespan."""
        return self.config.default_timespan

    @property
    def timezone(self):
        """Simple Get Timezone."""
        return self.config.default_tz

    @width.setter
    def width(self, new_width: int):
        """Simple Set Width.  Must redefine the config model for pydantic to validate."""
        new_config = GrafanaConfigSettings(
            grafana_url=self.config.grafana_url,
            grafana_api_key=self.config.grafana_api_key,
            default_width=new_width,
            default_height=self.config.default_height,
            default_theme=self.config.default_theme,
            default_timespan=self.config.default_timespan,
            grafana_org_id=self.config.grafana_org_id,
            default_tz=self.config.default_tz,
            config_file=self.config.config_file,
        )
        self.config = new_config

    @height.setter
    def height(self, new_height: int):
        """Simple Set Height.  Must redefine the config model for pydantic to validate."""
        new_config = GrafanaConfigSettings(
            grafana_url=self.config.grafana_url,
            grafana_api_key=self.config.grafana_api_key,
            default_width=self.config.default_width,
            default_height=new_height,
            default_theme=self.config.default_theme,
            default_timespan=self.config.default_timespan,
            grafana_org_id=self.config.grafana_org_id,
            default_tz=self.config.default_tz,
            config_file=self.config.config_file,
        )
        self.config = new_config

    @theme.setter
    def theme(self, new_theme: Literal["light", "dark"]):
        """Simple Set Theme.  Must redefine the config model for pydantic to validate."""
        new_config = GrafanaConfigSettings(
            grafana_url=self.config.grafana_url,
            grafana_api_key=self.config.grafana_api_key,
            default_width=self.config.default_width,
            default_height=self.config.default_height,
            default_theme=new_theme,
            default_timespan=self.config.default_timespan,
            grafana_org_id=self.config.grafana_org_id,
            default_tz=self.config.default_tz,
            config_file=self.config.config_file,
        )
        self.config = new_config

    @timespan.setter
    def timespan(self, new_timespan: str):
        """Simple Set Timespan.  Must redefine the config model for pydantic to validate."""
        new_config = GrafanaConfigSettings(
            grafana_url=self.config.grafana_url,
            grafana_api_key=self.config.grafana_api_key,
            default_width=self.config.default_width,
            default_height=self.config.default_height,
            default_theme=self.config.default_theme,
            default_timespan=new_timespan
            if not new_timespan
            else isodate.parse_duration(new_timespan).totimedelta(start=self.now),
            grafana_org_id=self.config.grafana_org_id,
            default_tz=self.config.default_tz,
            config_file=self.config.config_file,
        )
        self.config = new_config

    @timezone.setter
    def timezone(self, new_timezone: str):
        """Simple Set Timezone.  Must redefine the config model for pydantic to validate."""
        new_config = GrafanaConfigSettings(
            grafana_url=self.config.grafana_url,
            grafana_api_key=self.config.grafana_api_key,
            default_width=self.config.default_width,
            default_height=self.config.default_height,
            default_theme=self.config.default_theme,
            default_timespan=self.config.default_timespan,
            grafana_org_id=self.config.grafana_org_id,
            default_tz=new_timezone,
            config_file=self.config.config_file,
        )
        self.config = new_config

    def get_png(self, dashboard_slug: str, panel: dict) -> Union[bytes, None]:
        """Using requests GET the generated URL and return the binary contents of the file.

        Args:
            dashboard_slug (str): Grafana unique definition for a dashboard.
            panel (dict): Parsed panel items for this dashboard from panels.yml.

        Returns:
            Union[bytes, None]: The raw image from the renderer or None if there was an error.
        """
        url, payload = self.get_png_url(dashboard_slug, panel)
        headers = {"Authorization": f"Bearer {self.config.grafana_api_key}"}
        try:
            LOGGER.debug("Begin GET %s", url)
            results = requests.get(url, headers=headers, stream=True, params=payload, timeout=REQUEST_TIMEOUT_SEC)
        except RequestException as exc:
            LOGGER.error("An error occurred while accessing the url: %s Exception: %s", url, exc)
            return None

        if results.status_code == 200:
            LOGGER.debug("Request returned %s", results.status_code)
            return results.content

        LOGGER.error("Request returned %s for %s", results.status_code, url)
        return None

    def get_png_url(self, dashboard_slug: str, panel: dict) -> Tuple[str, dict]:
        """Generate the URL and the Payload for the request.

        Args:
            dashboard_slug (str): Grafana unique definition for a dashboard.
            panel (dict): Parsed panel items for this dashboard from panels.yml.

        Returns:
            Tuple[str, dict]: Grafana url and payload to send to the grafana renderer.
        """
        dashboard = next((i for i in self.panels["dashboards"] if i["dashboard_slug"] == dashboard_slug), None)
        payload = {
            "orgId": self.config.grafana_org_id,
            "panelId": panel["panel_id"],
            "tz": urllib.parse.quote(self.config.default_tz),
            "theme": self.config.default_theme,
        }
        from_time = str(int((self.now - self.config.default_timespan).timestamp() * 1e3))
        to_time = str(int(self.now.timestamp() * 1e3))
        if from_time != to_time:
            payload["from"] = from_time
            payload["to"] = to_time
        if self.config.default_width > 0:
            payload["width"] = self.config.default_width
        if self.config.default_height > 0:
            payload["height"] = self.config.default_height

        for variable in panel.get("variables", []):
            if variable.get("includeinurl", True) and variable.get("value"):
                payload[f"var-{variable['name']}"] = variable["value"]
        url = f"{self.config.grafana_url}/render/d-solo/{dashboard['dashboard_uid']}/{dashboard_slug}"
        LOGGER.debug("URL: %s Payload: %s", url, payload)
        return url, payload


handler = GrafanaHandler(PLUGIN_SETTINGS)
