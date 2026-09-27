import sys
import asyncio
import threading
import subprocess
import socket
import time
import os
import shutil


# =============================================================
# PACKAGED TUNNELD HELPER MODE
# =============================================================
# The PyInstaller EXE can relaunch itself with --tunneld. In
# helper mode we run pymobiledevice3's real tunneld CLI directly
# inside this bundled Python process. This avoids requiring a
# separate Python installation and avoids recursive helper EXEs.
if "--tunneld" in sys.argv:
    sys.argv = [
        "pymobiledevice3",
        "remote",
        "tunneld",
    ]
    from pymobiledevice3.__main__ import main as pymobiledevice3_main
    pymobiledevice3_main()
    raise SystemExit(0)

from PySide6.QtCore import QObject, Signal, Slot, QUrl, Qt
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QGroupBox,
    QDoubleSpinBox,
    QMessageBox,
    QCheckBox,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel

from pymobiledevice3.services.dvt.instruments import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import (
    LocationSimulation,
)
from pymobiledevice3.tunneld.api import get_tunneld_devices


TUNNEL_HOST = "127.0.0.1"
TUNNEL_PORT = 49151


class MapBridge(QObject):

    locationClicked = Signal(float, float)

    @Slot(float, float)
    def location_selected(self, latitude, longitude):
        self.locationClicked.emit(latitude, longitude)


class LocationApp(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("MyLocationApp")
        self.resize(1400, 900)

        # -------------------------------------------------
        # Connection state
        # -------------------------------------------------

        self.connected = False
        self.connecting = False

        self.devices = None
        self.rsd = None
        self.dvt = None

        self.location_context = None
        self.location_service = None

        self.tunneld_process = None

        # -------------------------------------------------
        # Location state
        # -------------------------------------------------

        self.current_latitude = None
        self.current_longitude = None

        self.initial_latitude = None
        self.initial_longitude = None

        self.map_dots = []

        # -------------------------------------------------
        # Route state
        # -------------------------------------------------

        self.route_running = False
        self.route_cancelled = False

        self.route_start_lat = None
        self.route_start_lon = None
        self.route_end_lat = None
        self.route_end_lon = None

        self.route_duration = 30.0
        self.route_mode = False
        self.route_click_stage = 0
        self.route_point_a = None
        self.route_point_b = None

        # -------------------------------------------------
        # Async event loop
        # -------------------------------------------------

        self.loop = asyncio.new_event_loop()

        self.loop_thread = threading.Thread(
            target=self.async_loop_thread,
            daemon=True,
        )

        self.loop_thread.start()

        # -------------------------------------------------
        # GUI
        # -------------------------------------------------

        self.create_gui()

    # =====================================================
    # ASYNC LOOP
    # =====================================================

    def async_loop_thread(self):

        asyncio.set_event_loop(self.loop)

        self.loop.run_forever()

    def run_async(self, coroutine, timeout=120):

        future = asyncio.run_coroutine_threadsafe(
            coroutine,
            self.loop,
        )

        return future.result(timeout=timeout)

    # =====================================================
    # GUI
    # =====================================================

    def create_gui(self):

        central = QWidget()

        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)

        # -------------------------------------------------
        # Title
        # -------------------------------------------------

        title = QLabel("MyLocationApp")

        title.setStyleSheet(
            """
            font-size: 26px;
            font-weight: bold;
            padding: 5px;
            """
        )

        main_layout.addWidget(title)

        # -------------------------------------------------
        # Connection
        # -------------------------------------------------

        connection_group = QGroupBox(
            "iPhone Connection"
        )

        connection_layout = QHBoxLayout(
            connection_group
        )

        self.status = QLabel(
            "● iPhone: Not Connected"
        )

        connection_layout.addWidget(
            self.status
        )

        self.connect_button = QPushButton(
            "Connect iPhone"
        )

        self.connect_button.clicked.connect(
            self.connect_phone
        )

        connection_layout.addWidget(
            self.connect_button
        )

        main_layout.addWidget(
            connection_group
        )

        # -------------------------------------------------
        # Map
        # -------------------------------------------------

        self.map_view = QWebEngineView()

        self.map_bridge = MapBridge()

        self.map_bridge.locationClicked.connect(
            self.map_clicked
        )

        self.channel = QWebChannel()

        self.channel.registerObject(
            "bridge",
            self.map_bridge
        )

        self.map_view.page().setWebChannel(
            self.channel
        )

        self.map_view.setHtml(
            self.create_map_html(),
            QUrl("http://localhost/"),
        )

        main_layout.addWidget(
            self.map_view,
            1,
        )

        # -------------------------------------------------
        # Map / Route mode controls
        # -------------------------------------------------

        dot_group = QGroupBox(
            "Map Locations"
        )

        dot_layout = QHBoxLayout(
            dot_group
        )

        self.dot_status = QLabel(
            "Selected location: None"
        )
        dot_layout.addWidget(self.dot_status)

        self.route_mode_button = QPushButton(
            "Route Mode: OFF"
        )
        self.route_mode_button.setCheckable(True)
        self.route_mode_button.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 12px; }"
            "QPushButton:checked { background-color: #333333; color: white; }"
        )
        self.route_mode_button.toggled.connect(
            self.toggle_route_mode
        )
        dot_layout.addWidget(self.route_mode_button)

        self.use_a_button = QPushButton(
            "Set Point A"
        )
        self.use_a_button.setStyleSheet(
            "QPushButton { background-color: #f44336; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #bdbdbd; color: #eeeeee; }"
        )
        self.use_a_button.clicked.connect(
            self.use_last_dot_as_a
        )
        self.use_a_button.setEnabled(False)
        dot_layout.addWidget(self.use_a_button)

        self.use_b_button = QPushButton(
            "Set Point B"
        )
        self.use_b_button.setStyleSheet(
            "QPushButton { background-color: #4caf50; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #bdbdbd; color: #eeeeee; }"
        )
        self.use_b_button.clicked.connect(
            self.use_last_dot_as_b
        )
        self.use_b_button.setEnabled(False)
        dot_layout.addWidget(self.use_b_button)

        self.clear_dots_button = QPushButton(
            "Clear Selection"
        )
        self.clear_dots_button.clicked.connect(
            self.clear_dots
        )
        dot_layout.addWidget(self.clear_dots_button)

        main_layout.addWidget(dot_group)

        marker_legend = QLabel(
            '<span style="color:#2196f3;font-size:18px;">●</span> '
            '<b>Blue = Selected Location</b>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#f44336;font-size:18px;">●</span> '
            '<b>Red = Point A</b>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#4caf50;font-size:18px;">●</span> '
            '<b>Green = Point B</b>'
        )
        marker_legend.setTextFormat(
            Qt.RichText
        )
        marker_legend.setStyleSheet(
            "padding: 4px 8px;"
        )
        main_layout.addWidget(marker_legend)

        # -------------------------------------------------
        # Manual location
        # -------------------------------------------------

        location_group = QGroupBox(
            "Location"
        )

        location_layout = QGridLayout(
            location_group
        )

        location_layout.addWidget(
            QLabel("Latitude"),
            0,
            0,
        )

        self.latitude_input = QLineEdit()

        location_layout.addWidget(
            self.latitude_input,
            0,
            1,
        )

        location_layout.addWidget(
            QLabel("Longitude"),
            0,
            2,
        )

        self.longitude_input = QLineEdit()

        location_layout.addWidget(
            self.longitude_input,
            0,
            3,
        )

        self.set_button = QPushButton(
            "Set Location"
        )

        self.set_button.setEnabled(False)

        self.set_button.clicked.connect(
            self.set_manual_location
        )

        location_layout.addWidget(
            self.set_button,
            0,
            4,
        )

        self.reset_button = QPushButton(
            "Reset Location"
        )

        self.reset_button.setEnabled(False)

        self.reset_button.clicked.connect(
            self.reset_location
        )

        location_layout.addWidget(
            self.reset_button,
            0,
            5,
        )

        main_layout.addWidget(
            location_group
        )

        # -------------------------------------------------
        # Current location
        # -------------------------------------------------

        self.current_location_status = QLabel(
            "Current simulated location (BLUE): None"
        )

        main_layout.addWidget(
            self.current_location_status
        )

        # -------------------------------------------------
        # Route
        # -------------------------------------------------

        self.route_group = QGroupBox(
            "A → B Route Simulation — Route Mode OFF"
        )

        route_layout = QGridLayout(
            self.route_group
        )

        route_layout.addWidget(
            QLabel("Point A Latitude"),
            0,
            0,
        )

        self.a_lat = QLineEdit()

        route_layout.addWidget(
            self.a_lat,
            0,
            1,
        )

        route_layout.addWidget(
            QLabel("Point A Longitude"),
            0,
            2,
        )

        self.a_lon = QLineEdit()

        route_layout.addWidget(
            self.a_lon,
            0,
            3,
        )

        route_layout.addWidget(
            QLabel("Point B Latitude"),
            1,
            0,
        )

        self.b_lat = QLineEdit()

        route_layout.addWidget(
            self.b_lat,
            1,
            1,
        )

        route_layout.addWidget(
            QLabel("Point B Longitude"),
            1,
            2,
        )

        self.b_lon = QLineEdit()

        route_layout.addWidget(
            self.b_lon,
            1,
            3,
        )

        route_layout.addWidget(
            QLabel("Travel Time"),
            2,
            0,
        )

        self.duration_box = QDoubleSpinBox()

        self.duration_box.setRange(
            1,
            86400,
        )

        self.duration_box.setValue(30)

        self.duration_box.setDecimals(1)

        self.duration_box.setSuffix(
            " seconds"
        )

        route_layout.addWidget(
            self.duration_box,
            2,
            1,
        )

        self.start_route_button = QPushButton(
            "Start A → B"
        )

        self.start_route_button.setEnabled(False)

        self.start_route_button.clicked.connect(
            self.start_route
        )

        route_layout.addWidget(
            self.start_route_button,
            2,
            2,
        )

        self.stop_route_button = QPushButton(
            "Stop Route"
        )

        self.stop_route_button.setEnabled(False)

        self.stop_route_button.clicked.connect(
            self.stop_route
        )

        route_layout.addWidget(
            self.stop_route_button,
            2,
            3,
        )

        self.route_widgets = [
            self.a_lat,
            self.a_lon,
            self.b_lat,
            self.b_lon,
            self.duration_box,
        ]

        for widget in self.route_widgets:
            widget.setEnabled(False)

        main_layout.addWidget(
            self.route_group
        )

        self.route_status = QLabel(
            "Route: Idle"
        )

        main_layout.addWidget(
            self.route_status
        )

    # =====================================================
    # MAP
    # =====================================================

    def create_map_html(self):

        return r"""
<!DOCTYPE html>

<html>

<head>

<meta charset="utf-8">

<meta name="viewport"
content="width=device-width, initial-scale=1.0">

<link
rel="stylesheet"
href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
/>

<style>

html,
body,
#map {
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 0;
}

</style>

</head>

<body>

<div id="map"></div>

<script
src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js">
</script>

<script
src="qrc:///qtwebchannel/qwebchannel.js">
</script>

<script>

var map =
    L.map("map").setView(
        [37.7749, -122.4194],
        12
    );

L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {
        maxZoom: 19,
        attribution:
            "&copy; OpenStreetMap contributors"
    }
).addTo(map);


var phoneMarker = null;

var mapDots = [];
var selectedLocationMarker = null;

var routeLine = null;

var routeStartMarker = null;

var routeEndMarker = null;


new QWebChannel(
    qt.webChannelTransport,
    function(channel) {

        window.bridge =
            channel.objects.bridge;

    }
);


map.on(
    "click",
    function(event) {

        if (!window.bridge) {
            return;
        }

        window.bridge.location_selected(
            event.latlng.lat,
            event.latlng.lng
        );

    }
);


window.setPhoneLocation =
function(latitude, longitude) {

    var position = [
        latitude,
        longitude
    ];


    if (!phoneMarker) {

        phoneMarker =
            L.circleMarker(
                position,
                {
                    radius: 10,
                    color: "#1565c0",
                    fillColor: "#2196f3",
                    fillOpacity: 0.9,
                    weight: 3
                }
            ).addTo(map);

    }
    else {

        phoneMarker.setLatLng(
            position
        );

    }


    phoneMarker.bindPopup(
        "<b>iPhone Location</b><br>" +
        latitude.toFixed(6) +
        ", " +
        longitude.toFixed(6)
    );

};


window.centerPhone =
function(latitude, longitude) {

    map.setView(
        [latitude, longitude],
        15
    );

};


window.addLocationDot =
function(latitude, longitude, number) {

    var position = [
        latitude,
        longitude
    ];

    if (!selectedLocationMarker) {

        selectedLocationMarker =
            L.circleMarker(
                position,
                {
                    radius: 9,
                    color: "#0d47a1",
                    fillColor: "#2196f3",
                    fillOpacity: 1,
                    weight: 3
                }
            ).addTo(map);

    }
    else {

        selectedLocationMarker.setLatLng(
            position
        );

    }

    selectedLocationMarker.bindPopup(
        "<b>Selected Location</b><br>" +
        latitude.toFixed(6) +
        ", " +
        longitude.toFixed(6)
    );

};


window.clearLocationDots =
function() {

    for (
        var i = 0;
        i < mapDots.length;
        i++
    ) {

        map.removeLayer(
            mapDots[i]
        );

    }

    mapDots = [];

    if (selectedLocationMarker) {
        map.removeLayer(selectedLocationMarker);
        selectedLocationMarker = null;
    }

};


window.setRoutePointA =
function(latitude, longitude) {

    if (routeStartMarker) {
        map.removeLayer(routeStartMarker);
    }

    routeStartMarker =
        L.circleMarker(
            [latitude, longitude],
            {
                radius: 9,
                color: "#8b0000",
                fillColor: "#f44336",
                fillOpacity: 1,
                weight: 3
            }
        ).addTo(map);

    routeStartMarker.bindPopup(
        "<b>Point A</b><br>" +
        latitude.toFixed(6) + ", " +
        longitude.toFixed(6)
    );
};


window.setRoutePointB =
function(latitude, longitude) {

    if (routeEndMarker) {
        map.removeLayer(routeEndMarker);
    }

    routeEndMarker =
        L.circleMarker(
            [latitude, longitude],
            {
                radius: 9,
                color: "#1b5e20",
                fillColor: "#4caf50",
                fillOpacity: 1,
                weight: 3
            }
        ).addTo(map);

    routeEndMarker.bindPopup(
        "<b>Point B</b><br>" +
        latitude.toFixed(6) + ", " +
        longitude.toFixed(6)
    );
};


window.clearRoute =
function() {

    if (routeLine) {
        map.removeLayer(routeLine);
        routeLine = null;
    }

    if (routeStartMarker) {
        map.removeLayer(routeStartMarker);
        routeStartMarker = null;
    }

    if (routeEndMarker) {
        map.removeLayer(routeEndMarker);
        routeEndMarker = null;
    }
};


window.drawRoute =
function(
    aLat,
    aLon,
    bLat,
    bLon
) {

    if (routeLine) {
        map.removeLayer(routeLine);
    }

    if (routeStartMarker) {
        map.removeLayer(routeStartMarker);
    }

    if (routeEndMarker) {
        map.removeLayer(routeEndMarker);
    }


    routeLine =
        L.polyline(
            [
                [aLat, aLon],
                [bLat, bLon]
            ],
            {
                weight: 5
            }
        ).addTo(map);


    routeStartMarker =
        L.circleMarker(
            [aLat, aLon],
            {
                radius: 9,
                color: "#8b0000",
                fillColor: "#f44336",
                fillOpacity: 1,
                weight: 3
            }
        ).addTo(map);


    routeEndMarker =
        L.circleMarker(
            [bLat, bLon],
            {
                radius: 9,
                color: "#1b5e20",
                fillColor: "#4caf50",
                fillOpacity: 1,
                weight: 3
            }
        ).addTo(map);


    map.fitBounds(
        routeLine.getBounds(),
        {
            padding: [40, 40]
        }
    );

};

</script>

</body>

</html>
"""

    # =====================================================
    # TUNNELD CHECK
    # =====================================================

    def is_tunneld_running(self):

        try:

            sock = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )

            sock.settimeout(0.75)

            result = sock.connect_ex(
                (
                    TUNNEL_HOST,
                    TUNNEL_PORT,
                )
            )

            sock.close()

            return result == 0

        except Exception:

            return False

    # =====================================================
    # START TUNNELD
    # =====================================================

    def start_tunneld(self):

        # If another valid tunneld is already listening, use it.
        if self.is_tunneld_running():
            print("tunneld is already running.")
            return True

        print("tunneld is not running.")
        print("Starting bundled pymobiledevice3 tunneld...")

        # IMPORTANT:
        # In a PyInstaller build, sys.executable is MyLocationApp.exe.
        # We intentionally relaunch the SAME executable with --tunneld.
        # The top-of-file helper-mode branch then runs pymobiledevice3's
        # real tunneld CLI instead of starting the GUI again.
        # When running from source, Python needs the script path before
        # the helper argument. When frozen by PyInstaller, the EXE itself
        # is the program and --tunneld can be passed directly to it.
        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "--tunneld",
            ]
        else:
            command = [
                sys.executable,
                os.path.abspath(__file__),
                "--tunneld",
            ]

        creation_flags = 0

        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        try:
            self.tunneld_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creation_flags,
                cwd=os.path.dirname(sys.executable),
            )
        except Exception as error:
            raise RuntimeError(
                "Could not start the bundled pymobiledevice3 "
                "tunneld.\n\n" + str(error)
            )

        # Wait up to 30 seconds for the HTTP tunneld service.
        for _ in range(60):
            if self.is_tunneld_running():
                print("tunneld started successfully.")
                return True

            # If the helper exited immediately, report that instead of
            # making the user wait the entire timeout.
            if self.tunneld_process.poll() is not None:
                return_code = self.tunneld_process.returncode
                raise RuntimeError(
                    "The bundled tunneld process exited before it "
                    "started.\n\n"
                    f"Exit code: {return_code}\n\n"
                    "Try running the EXE from an Administrator Command "
                    "Prompt if Windows permissions are preventing the "
                    "iPhone tunnel from being created."
                )

            time.sleep(0.5)

        try:
            if self.tunneld_process and self.tunneld_process.poll() is None:
                self.tunneld_process.terminate()
        except Exception:
            pass

        raise RuntimeError(
            "tunneld did not start within 30 seconds.\n\n"
            "Make sure the iPhone is connected, unlocked, trusted, "
            "and that Windows has the required Apple device drivers."
        )

    # =====================================================
    # CONNECTION
    # =====================================================

    def connect_phone(self):

        if self.connected:

            return


        if self.connecting:

            return


        self.connecting = True

        self.connect_button.setEnabled(
            False
        )

        self.status.setText(
            "● Starting/checking tunneld..."
        )

        QApplication.processEvents()


        try:

            self.start_tunneld()

            self.status.setText(
                "● Looking for iPhone..."
            )

            QApplication.processEvents()


            self.run_async(
                self._connect_phone(),
                timeout=120,
            )


            self.connected = True

            self.status.setText(
                "● iPhone: Connected"
            )


            self.set_button.setEnabled(
                True
            )

            self.reset_button.setEnabled(
                True
            )

            self.start_route_button.setEnabled(
                self.route_mode
            )


        except Exception as error:

            self.connected = False

            self.status.setText(
                "● iPhone: Connection Failed"
            )

            QMessageBox.critical(
                self,
                "iPhone Connection Error",
                str(error),
            )

        finally:

            self.connecting = False

            self.connect_button.setEnabled(
                True
            )

    # =====================================================
    # CONNECT TO DEVICE
    # =====================================================

    async def _connect_phone(self):

        print(
            "Looking for iPhone..."
        )


        # tunneld may already be listening before the iPhone tunnel/device
        # is fully ready. Retry discovery so the first Connect click does
        # not fail simply because initialization needs a few more seconds.
        devices = []

        discovery_attempts = 20
        discovery_delay = 1.0

        for attempt in range(1, discovery_attempts + 1):

            try:
                devices = await get_tunneld_devices()
            except Exception as error:
                print(
                    f"Device discovery attempt {attempt}/"
                    f"{discovery_attempts} failed:",
                    error,
                )
                devices = []

            if devices:
                break

            print(
                f"Waiting for iPhone tunnel/device... "
                f"({attempt}/{discovery_attempts})"
            )

            await asyncio.sleep(
                discovery_delay
            )


        if not devices:

            raise RuntimeError(
                "No iPhone was found after waiting for the "
                "connection to finish initializing.\n\n"
                "Check that:\n"
                "• The iPhone is connected.\n"
                "• The iPhone is unlocked.\n"
                "• The computer is trusted.\n"
                "• Apple device drivers are installed.\n"
                "• tunneld is running."
            )


        print(
            f"Found {len(devices)} device(s)."
        )


        self.devices = devices

        self.rsd = devices[0]


        print(
            "Connecting to developer services..."
        )


        self.dvt = DvtProvider(
            self.rsd
        )


        self.location_context = (
            LocationSimulation(
                self.dvt
            )
        )


        self.location_service = (
            await self.location_context.__aenter__()
        )


        print(
            "Location simulation connected!"
        )

    # =====================================================
    # ROUTE MODE
    # =====================================================

    def toggle_route_mode(self, enabled):

        if self.route_running:
            self.stop_route()

        self.route_mode = bool(enabled)
        self.route_click_stage = 0

        self.route_mode_button.setText(
            "Route Mode: ON"
            if self.route_mode
            else "Route Mode: OFF"
        )

        self.route_group.setTitle(
            "A → B Route Simulation — Route Mode ON"
            if self.route_mode
            else "A → B Route Simulation — Route Mode OFF"
        )

        for widget in self.route_widgets:
            widget.setEnabled(self.route_mode)

        self.use_a_button.setEnabled(self.route_mode)
        self.use_b_button.setEnabled(self.route_mode)

        self.start_route_button.setEnabled(
            self.connected and self.route_mode
        )
        self.stop_route_button.setEnabled(False)

        if self.route_mode:

            self.route_point_a = None
            self.route_point_b = None
            self.a_lat.clear()
            self.a_lon.clear()
            self.b_lat.clear()
            self.b_lon.clear()

            self.map_view.page().runJavaScript(
                "clearLocationDots(); clearRoute();"
            )

            self.map_dots.clear()
            self.dot_status.setText(
                "Route Mode: click the map for RED Point A"
            )
            self.route_status.setText(
                "Route: Select Point A"
            )

        else:

            self.route_point_a = None
            self.route_point_b = None

            self.map_view.page().runJavaScript(
                "clearRoute();"
            )

            self.route_status.setText(
                "Route: Idle"
            )
            self.dot_status.setText(
                "Selected location: None"
            )


    # =====================================================
    # MAP CLICK
    # =====================================================

    def map_clicked(
        self,
        latitude,
        longitude,
    ):

        latitude = float(latitude)
        longitude = float(longitude)

        # Route Mode: first click is A, second click is B.
        # Further clicks start a fresh A -> B selection.
        if self.route_mode:

            self.map_dots = [
                (latitude, longitude)
            ]

            if self.route_click_stage == 0:

                self.route_point_a = (
                    latitude,
                    longitude,
                )
                self.route_point_b = None

                self.a_lat.setText(
                    f"{latitude:.6f}"
                )
                self.a_lon.setText(
                    f"{longitude:.6f}"
                )
                self.b_lat.clear()
                self.b_lon.clear()

                self.map_view.page().runJavaScript(
                    "clearRoute();"
                    "setRoutePointA("
                    f"{latitude},{longitude}"
                    ");"
                )

                self.route_click_stage = 1

                self.dot_status.setText(
                    "Point A selected (RED). "
                    "Click the map for GREEN Point B."
                )
                self.route_status.setText(
                    "Route: Select Point B"
                )

            else:

                self.route_point_b = (
                    latitude,
                    longitude,
                )

                self.b_lat.setText(
                    f"{latitude:.6f}"
                )
                self.b_lon.setText(
                    f"{longitude:.6f}"
                )

                a_lat, a_lon = self.route_point_a

                self.map_view.page().runJavaScript(
                    "setRoutePointB("
                    f"{latitude},{longitude}"
                    ");"
                    "drawRoute("
                    f"{a_lat},{a_lon},"
                    f"{latitude},{longitude}"
                    ");"
                )

                self.route_click_stage = 0

                self.dot_status.setText(
                    "Route selected: RED A → GREEN B. "
                    "Next click starts a new Point A."
                )
                self.route_status.setText(
                    "Route: Ready"
                )

            return

        # Normal mode: keep exactly one movable blue selection marker.
        self.map_dots = [
            (
                latitude,
                longitude,
            )
        ]

        self.latitude_input.setText(
            f"{latitude:.6f}"
        )
        self.longitude_input.setText(
            f"{longitude:.6f}"
        )

        self.map_view.page().runJavaScript(
            "addLocationDot("
            f"{latitude},"
            f"{longitude},"
            "1"
            ");"
        )

        self.dot_status.setText(
            "Selected location (BLUE): "
            f"{latitude:.6f}, {longitude:.6f}"
        )


    # =====================================================
    # SET LAST SELECTED LOCATION AS A
    # =====================================================

    def use_last_dot_as_a(self):

        if not self.route_mode or not self.map_dots:
            return

        latitude, longitude = self.map_dots[-1]

        self.route_point_a = (
            latitude,
            longitude,
        )

        self.a_lat.setText(
            f"{latitude:.6f}"
        )
        self.a_lon.setText(
            f"{longitude:.6f}"
        )

        self.map_view.page().runJavaScript(
            "setRoutePointA("
            f"{latitude},{longitude}"
            ");"
        )


    # =====================================================
    # SET LAST SELECTED LOCATION AS B
    # =====================================================

    def use_last_dot_as_b(self):

        if not self.route_mode or not self.map_dots:
            return

        latitude, longitude = self.map_dots[-1]

        self.route_point_b = (
            latitude,
            longitude,
        )

        self.b_lat.setText(
            f"{latitude:.6f}"
        )
        self.b_lon.setText(
            f"{longitude:.6f}"
        )

        self.map_view.page().runJavaScript(
            "setRoutePointB("
            f"{latitude},{longitude}"
            ");"
        )

        if self.route_point_a:
            a_lat, a_lon = self.route_point_a
            self.map_view.page().runJavaScript(
                "drawRoute("
                f"{a_lat},{a_lon},"
                f"{latitude},{longitude}"
                ");"
            )


    # =====================================================
    # CLEAR SELECTION
    # =====================================================

    def clear_dots(self):

        self.map_dots.clear()

        if self.route_mode:

            self.route_point_a = None
            self.route_point_b = None
            self.route_click_stage = 0

            self.a_lat.clear()
            self.a_lon.clear()
            self.b_lat.clear()
            self.b_lon.clear()

            self.map_view.page().runJavaScript(
                "clearRoute();"
            )

            self.dot_status.setText(
                "Route Mode: click the map for RED Point A"
            )
            self.route_status.setText(
                "Route: Select Point A"
            )

        else:

            self.map_view.page().runJavaScript(
                "clearLocationDots();"
            )

            self.dot_status.setText(
                "Selected location: None"
            )

    # =====================================================
    # MANUAL LOCATION
    # =====================================================

    def set_manual_location(self):

        if not self.connected:

            QMessageBox.warning(
                self,
                "Not Connected",
                "Connect the iPhone first.",
            )

            return


        try:

            latitude = float(
                self.latitude_input.text()
            )

            longitude = float(
                self.longitude_input.text()
            )

        except ValueError:

            QMessageBox.warning(
                self,
                "Invalid Location",
                "Enter valid latitude and longitude.",
            )

            return


        if not -90 <= latitude <= 90:

            QMessageBox.warning(
                self,
                "Invalid Latitude",
                "Latitude must be between -90 and 90.",
            )

            return


        if not -180 <= longitude <= 180:

            QMessageBox.warning(
                self,
                "Invalid Longitude",
                "Longitude must be between -180 and 180.",
            )

            return


        try:

            self.send_location(
                latitude,
                longitude,
            )

        except Exception as error:

            QMessageBox.critical(
                self,
                "Location Error",
                str(error),
            )

    # =====================================================
    # SEND LOCATION
    # =====================================================

    def send_location_to_device(
        self,
        latitude,
        longitude,
    ):

        if not self.location_service:
            raise RuntimeError(
                "Location simulation is not connected."
            )

        async def send():

            await self.location_service.set(
                latitude,
                longitude,
            )

        self.run_async(
            send(),
            timeout=30,
        )


    def update_simulated_location_gui(
        self,
        latitude,
        longitude,
    ):

        self.current_latitude = latitude
        self.current_longitude = longitude

        self.current_location_status.setText(
            "Current simulated location (BLUE): "
            f"{latitude:.6f}, "
            f"{longitude:.6f}"
        )

        self.map_view.page().runJavaScript(
            "setPhoneLocation("
            f"{latitude},"
            f"{longitude}"
            ");"
        )


    def send_location(
        self,
        latitude,
        longitude,
    ):

        self.send_location_to_device(
            latitude,
            longitude,
        )

        self.update_simulated_location_gui(
            latitude,
            longitude,
        )

    # =====================================================
    # RESET LOCATION
    # =====================================================

    def reset_location(self):

        if not self.connected:

            QMessageBox.warning(
                self,
                "Not Connected",
                "Connect the iPhone first.",
            )

            return

        try:

            # Stop any route that may currently be running.
            self.route_cancelled = True
            self.route_running = False

            # Clear iOS location simulation regardless of whether
            # an original connection location was recorded.
            async def clear_simulation():

                if not self.location_service:
                    raise RuntimeError(
                        "Location simulation is not connected."
                    )

                await self.location_service.clear()

            self.run_async(
                clear_simulation(),
                timeout=30,
            )

            self.current_latitude = None
            self.current_longitude = None

            self.latitude_input.clear()
            self.longitude_input.clear()

            self.current_location_status.setText(
                "Current simulated location (BLUE): None"
            )

            self.route_status.setText(
                "Route: Idle"
            )

            self.stop_route_button.setEnabled(
                False
            )

            self.start_route_button.setEnabled(
                self.connected and self.route_mode
            )

            # Remove the app's blue simulated-phone marker.
            self.map_view.page().runJavaScript(
                """
                if (phoneMarker) {
                    map.removeLayer(phoneMarker);
                    phoneMarker = null;
                }
                """
            )

        except Exception as error:

            QMessageBox.critical(
                self,
                "Reset Error",
                str(error),
            )

    # =====================================================
    # ROUTE
    # =====================================================

    def start_route(self):

        if not self.route_mode:

            QMessageBox.warning(
                self,
                "Route Mode Off",
                "Turn Route Mode ON before starting a route.",
            )
            return

        if not self.connected:

            QMessageBox.warning(
                self,
                "Not Connected",
                "Connect the iPhone first.",
            )

            return


        try:

            a_lat = float(
                self.a_lat.text()
            )

            a_lon = float(
                self.a_lon.text()
            )

            b_lat = float(
                self.b_lat.text()
            )

            b_lon = float(
                self.b_lon.text()
            )

            duration = float(
                self.duration_box.value()
            )

        except ValueError:

            QMessageBox.warning(
                self,
                "Invalid Route",
                "Enter valid Point A and Point B coordinates.",
            )

            return


        if not -90 <= a_lat <= 90:
            return

        if not -90 <= b_lat <= 90:
            return

        if not -180 <= a_lon <= 180:
            return

        if not -180 <= b_lon <= 180:
            return


        if self.route_running:

            return


        self.route_start_lat = a_lat
        self.route_start_lon = a_lon

        self.route_end_lat = b_lat
        self.route_end_lon = b_lon

        self.route_duration = duration

        self.route_cancelled = False

        self.route_running = True


        self.start_route_button.setEnabled(
            False
        )

        self.stop_route_button.setEnabled(
            True
        )


        self.map_view.page().runJavaScript(
            "drawRoute("
            f"{a_lat},"
            f"{a_lon},"
            f"{b_lat},"
            f"{b_lon}"
            ");"
        )


        threading.Thread(
            target=self.route_worker,
            daemon=True,
        ).start()

    # =====================================================
    # ROUTE WORKER
    # =====================================================

    def route_worker(self):

        start_time = time.time()
        duration = max(0.1, self.route_duration)
        route_error = None

        while True:

            if self.route_cancelled:
                break

            elapsed = time.time() - start_time
            progress = min(1.0, elapsed / duration)

            latitude = (
                self.route_start_lat
                + (
                    self.route_end_lat
                    - self.route_start_lat
                )
                * progress
            )

            longitude = (
                self.route_start_lon
                + (
                    self.route_end_lon
                    - self.route_start_lon
                )
                * progress
            )

            try:

                # IMPORTANT: worker thread only communicates with the
                # device. It does not touch Qt widgets or QWebEngine.
                self.send_location_to_device(
                    latitude,
                    longitude,
                )

            except Exception as error:

                route_error = str(error)
                print(
                    "Route location error:",
                    error,
                )
                break

            remaining = max(
                0,
                duration - elapsed,
            )

            QApplication.instance().postEvent(
                self,
                RouteStatusEvent(
                    progress,
                    remaining,
                    latitude,
                    longitude,
                ),
            )

            if progress >= 1:
                break

            time.sleep(0.5)

        self.route_running = False

        QApplication.instance().postEvent(
            self,
            RouteFinishedEvent(
                self.route_cancelled,
                route_error,
            ),
        )

    # =====================================================
    # STOP ROUTE
    # =====================================================

    def stop_route(self):

        self.route_cancelled = True

        self.route_running = False

        self.stop_route_button.setEnabled(
            False
        )

        self.start_route_button.setEnabled(
            self.connected and self.route_mode
        )

        self.route_status.setText(
            "Route: Stopped"
        )

    # =====================================================
    # CLOSE
    # =====================================================

    def closeEvent(self, event):

        self.route_cancelled = True

        self.route_running = False


        try:

            if self.location_context:

                async def cleanup():

                    try:

                        await (
                            self.location_context.__aexit__(
                                None,
                                None,
                                None,
                            )
                        )

                    except Exception:
                        pass


                self.run_async(
                    cleanup(),
                    timeout=10,
                )

        except Exception:

            pass


        try:

            if self.tunneld_process:

                self.tunneld_process.terminate()

        except Exception:

            pass


        try:

            self.loop.call_soon_threadsafe(
                self.loop.stop
            )

        except Exception:

            pass


        event.accept()


# =========================================================
# ROUTE GUI EVENTS
# =========================================================

from PySide6.QtCore import QEvent


class RouteStatusEvent(QEvent):

    EVENT_TYPE = QEvent.Type(
        QEvent.registerEventType()
    )

    def __init__(
        self,
        progress,
        remaining,
        latitude,
        longitude,
    ):

        super().__init__(
            self.EVENT_TYPE
        )

        self.progress = progress
        self.remaining = remaining
        self.latitude = latitude
        self.longitude = longitude


class RouteFinishedEvent(QEvent):

    EVENT_TYPE = QEvent.Type(
        QEvent.registerEventType()
    )

    def __init__(
        self,
        cancelled,
        error=None,
    ):

        super().__init__(
            self.EVENT_TYPE
        )

        self.cancelled = cancelled
        self.error = error


# =========================================================
# EVENT HANDLER
# =========================================================

_original_event = LocationApp.event


def location_app_event(
    self,
    event,
):

    if isinstance(
        event,
        RouteStatusEvent,
    ):

        self.update_simulated_location_gui(
            event.latitude,
            event.longitude,
        )

        self.route_status.setText(
            "Route: "
            f"{event.progress * 100:.1f}%   "
            f"Remaining: "
            f"{event.remaining:.1f}s"
        )

        return True


    if isinstance(
        event,
        RouteFinishedEvent,
    ):

        self.route_running = False

        self.stop_route_button.setEnabled(
            False
        )

        self.start_route_button.setEnabled(
            self.connected and self.route_mode
        )


        if event.cancelled:

            self.route_status.setText(
                "Route: Stopped"
            )

        else:

            self.route_status.setText(
                "Route: Arrived at Point B"
            )

        return True


    return _original_event(
        self,
        event,
    )


LocationApp.event = location_app_event


# =========================================================
# APPLICATION START
# =========================================================

if __name__ == "__main__":

    app = QApplication(
        sys.argv
    )

    window = LocationApp()

    window.show()

    sys.exit(
        app.exec()
    )