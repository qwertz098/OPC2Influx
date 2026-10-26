# Deploying OpcUA_2_InfluxDB on the IXON SecureEdge Pro

This guide covers building and deploying the application as a Docker container on the IXON SecureEdge Pro (SEP) edge gateway.

---

## Overview

The SecureEdge Pro runs Docker on **ARM64** (`linux/arm64/v8`). It has a built-in private Docker registry at `192.168.140.1:5000` (HTTP, no TLS). You cross-compile the image on your development machine using `docker buildx`, push it to the gateway's local registry, then create and start the container via the gateway's web interface.

```
 Dev Machine                   SecureEdge Pro
 ┌─────────────┐   push        ┌──────────────────┐
 │ docker      │-------------->│ Registry :5000   │
 │ buildx      │  (HTTP)       │                  │
 │ ARM64 image │               │ Docker Engine    │
 └─────────────┘               │  └─ container    │
       │                       │    opcua2influx  │
       │                       │     :8000        │
  LAN / USB-Ethernet           └──────────────────┘
```

---

## Prerequisites

### Hardware

- IXON SecureEdge Pro with Docker support enabled
- Wired Ethernet connection between your dev machine and the gateway (default gateway IP: `192.168.140.1`)

### Software

- **Docker Desktop** (Windows / macOS) or **Docker Engine** (Linux) with **buildx** support
- buildx is included by default in Docker Desktop 4.x+ and Docker Engine 23.0+

### Network

The default gateway IP is `192.168.140.1`. Your gateway may use a different IP — check your IXON Cloud portal or the gateway's network settings. Throughout this guide, replace `192.168.140.1` with your actual gateway IP wherever it appears.

Your dev machine must be able to reach the gateway's registry. Verify with:

```
curl http://<gateway-ip>:5000/v2/
```

You should get `{}` or a JSON response. If the connection is refused, check your Ethernet adapter and ensure you are on the same subnet as the gateway.

---

## Step 1: Configure Docker for the Insecure Registry

The SEP registry uses plain HTTP (no TLS). Docker refuses to push to HTTP registries by default. You must whitelist it.

### Windows (Docker Desktop)

1. Open **Docker Desktop** → **Settings** → **Docker Engine**
2. Add your gateway IP to the `insecure-registries` array (default `192.168.140.1`):

```json
{
  "insecure-registries": ["<gateway-ip>:5000"]
}
```

3. Click **Apply & Restart**

### Linux (Docker Engine)

1. Edit or create `/etc/docker/daemon.json`:

```json
{
  "insecure-registries": ["<gateway-ip>:5000"]
}
```

2. Restart Docker:

```bash
sudo systemctl restart docker
```

### macOS (Docker Desktop)

Same as Windows: **Docker Desktop** → **Settings** → **Docker Engine** → add to `insecure-registries`.

---

## Step 2: Build and Push the ARM64 Image

The application must be cross-compiled for `linux/arm64/v8` since your dev machine is most likely x86_64.

### Option A: Use the Provided Scripts

The repository includes ready-made build scripts that handle buildx setup automatically.

**Windows:**

```cmd
build_and_push_edge.cmd
```

**Linux / macOS:**

```bash
chmod +x build_and_push_edge.sh
./build_and_push_edge.sh
```

Both scripts:
1. Create a buildx builder named `secure-edge-pro` (with insecure registry config from `buildkitd-secure-edge-pro.toml`)
2. Cross-compile the Docker image for `linux/arm64/v8`
3. Push the image to `<gateway-ip>:5000/opcua2influxdb:latest`

To use a different gateway IP or image name, set environment variables before running:

**Linux / macOS:**
```bash
EDGE_IP=10.0.0.50 IMAGE_NAME=myapp IMAGE_TAG=v2 ./build_and_push_edge.sh
```

**Windows:**
Edit the `set` lines at the top of `build_and_push_edge.cmd`.

### Option B: Manual Commands

If you prefer to run the commands yourself:

```bash
# 1. Create a buildx builder with insecure registry support
docker buildx create --name secure-edge-pro \
  --config buildkitd-secure-edge-pro.toml

# 2. Select the builder
docker buildx use secure-edge-pro

# 3. Build for ARM64 and push to the gateway
docker buildx build \
  --platform linux/arm64/v8 \
  --tag <gateway-ip>:5000/opcua2influxdb:latest \
  --push .
```

### Verify the Push

Confirm the image is in the registry:

```bash
curl http://<gateway-ip>:5000/v2/_catalog
```

Expected output:

```json
{"repositories":["opcua2influxdb"]}
```

---

## Step 3: Create the Container on the Gateway

1. Open the gateway web interface at **http://\<gateway-ip\>** in your browser
2. Navigate to **Docker** → **Images** — you should see `opcua2influxdb:latest`
3. Click **Create Container** and configure:

### Container Settings

| Setting | Value |
|---|---|
| **Image** | `opcua2influxdb:latest` |
| **Container name** | `opcua2influxdb` (or any name) |
| **Restart policy** | `unless-stopped` |

### Networking

Choose **Bridge** networking. This gives the container its own IP on the gateway's internal Docker network while allowing it to reach external OPC UA servers and InfluxDB instances via the gateway's network interfaces.

| Setting | Value |
|---|---|
| **Network mode** | Bridge |
| **Port mapping** | `8000` → `8000` (TCP) |

Do **not** use a port that is already in use by the gateway or by configured port forwards (e.g. 443, 80).

### Volumes

The SEP does **not** support bind mounts (mounting host directories). Use **named volumes** only.

| Volume name | Mount path | Purpose |
|---|---|---|
| `opcua2influx_config` | `/config` | YAML configuration files |
| `opcua2influx_data` | `/data` | SQLite database files |

These volumes persist across container restarts and image updates. Your configuration and recorded data survive even if you recreate the container with a newer image.

### Environment Variables

| Variable | Required | Example | Description |
|---|---|---|---|
| `INFLUXDB_TOKEN` | If using InfluxDB | `your-token-here` | InfluxDB authentication token |
| `APP_PORT` | No | `8000` | HTTP port (default: 8000) |
| `SMTP_PASSWORD` | If using alerts | `smtp-pass` | SMTP password for email alerts |

4. Click **Start** to launch the container

---

## Step 4: Access the Application

Once the container is running, open a browser and navigate to:

```
http://<gateway-ip>:8000
```

You should see the OpcUA_2_InfluxDB dashboard. From here you can:

1. Add your OPC UA servers (use IPs reachable from the gateway's network)
2. Configure InfluxDB targets
3. Browse OPC UA nodes and create subscriptions

---

## SEP Constraints and Limitations

The SecureEdge Pro Docker environment has specific restrictions compared to a standard Docker host. All of these are already handled by this application's Dockerfile and compose setup, but keep them in mind if you modify the deployment.

### No `cap_add` or `privileged`

The SEP does not support `cap_add`, `cap_drop`, or `privileged` mode. The application does not require any elevated capabilities.

### No Bind Mounts

Host filesystem access via bind mounts (`-v /host/path:/container/path`) is not available. Always use named Docker volumes. The Dockerfile declares `VOLUME ["/config", "/data"]` which works correctly with named volumes.

### Data Persistence

Named volumes are the only way to persist data. When you delete a container, the named volumes remain. When you recreate a container from an updated image, reattach the same volumes to preserve your configuration and data.

To pre-populate configuration: start the container once (it creates default config files in `/config`), then stop it, modify the config via the web UI, and restart.

### Networking

| Mode | Description | Use case |
|---|---|---|
| **Bridge** | Container gets its own IP, port mapping required | Recommended — isolated, predictable |
| **Host** | Container shares the gateway's network stack | Use only if bridge port mapping causes issues |
| **None** | No networking | Not useful for this application |

With Bridge networking, the container can resolve other containers by name (DNS). If you run InfluxDB as a second container on the same gateway, use the container name as the hostname (e.g. `http://influxdb:8086`).

### Port Conflicts

The gateway reserves certain ports for its own services and for configured port forwards. If port 8000 conflicts, set the `APP_PORT` environment variable to a different port and update the port mapping accordingly.

### Storage Space

The SEP has limited disk space. Monitor volume usage. SQLite databases in `/data` can grow over time — consider configuring retention or periodic cleanup.

---

## Updating the Application

To deploy a new version:

1. On your dev machine, pull or build the latest code
2. Run the build script again (`build_and_push_edge.cmd` or `./build_and_push_edge.sh`)
3. On the gateway web interface:
   - Stop the running container
   - Delete the container (volumes are preserved)
   - Pull/refresh the image
   - Create a new container with the same settings and volumes
   - Start the container

Your configuration and data persist in the named volumes — no reconfiguration needed.

---

## Troubleshooting

### "connection refused" when pushing

- Verify the Ethernet connection: `ping <gateway-ip>`
- Ensure Docker Desktop has `<gateway-ip>:5000` in `insecure-registries` and has been restarted
- Check that the SEP's Docker service is running

### Build fails with "no matching manifest for linux/arm64"

The base image `python:3.12-slim` supports ARM64. If you see this error:
- Ensure you are using `docker buildx build` (not `docker build`)
- Verify the builder is active: `docker buildx inspect secure-edge-pro`

### Container starts but web UI is not reachable

- Check the port mapping in the container config on the gateway
- Try Host networking mode to rule out port mapping issues
- Check container logs via the gateway web interface for startup errors

### OPC UA servers not reachable from the container

- OPC UA server endpoints must use IPs routable from the gateway's network
- `localhost` or `127.0.0.1` inside the container refers to the container itself, not the gateway
- Use the gateway's LAN IP or the OPC UA server's actual network IP

### InfluxDB connection fails

- If InfluxDB runs on a remote server, ensure the gateway can reach it (check firewall rules)
- If InfluxDB runs as another container on the same gateway, use Bridge networking and reference it by container name
- Verify the `INFLUXDB_TOKEN` environment variable is set correctly

---

## Quick Reference

```bash
# Build and push (Linux/macOS)
./build_and_push_edge.sh

# Build and push (Windows)
build_and_push_edge.cmd

# Verify image is in registry
curl http://<gateway-ip>:5000/v2/_catalog

# Check image tags
curl http://<gateway-ip>:5000/v2/opcua2influxdb/tags/list

# Access the app
# http://<gateway-ip>:8000
```
