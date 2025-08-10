# app.py
import os, subprocess, json, random, string, time
from textwrap import dedent
import streamlit as st

# Utilities
def sh(cmd, timeout=60):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout)
        return 0, out.decode()
    except subprocess.CalledProcessError as e:
        return e.returncode, e.output.decode()
    except Exception as e:
        return 1, str(e)

def yaml_separator():
    return "---\n"

def gen_name(prefix):
    return f"{prefix}-{''.join(random.choices(string.asciiLowercase + string.digits, k=5))}"

def safe(val, default):
    return val if val else default

st.set_page_config(page_title="Kubernetes Control Panel", layout="wide")

st.sidebar.title("K8s Control Panel")
namespace = st.sidebar.text_input("Namespace", value="default")
cluster_context = st.sidebar.text_input("kubectl context (optional)", value="")
context_flag = f"--context {cluster_context}" if cluster_context.strip() else ""
st.sidebar.caption("Ensure kubectl is configured and has permissions for the selected namespace.")

st.title("Kubernetes Multi-Feature Panel")
st.write("Deploy multi-tier apps and a live streaming stack to your Kubernetes cluster, monitor status, and test endpoints.")

tab1, tab2, tab3 = st.tabs(["Multi-tier Websites", "Live Streaming (RTMP/HLS)", "Cluster Status"])

# ---------- Templates ----------
def tmpl_web_deployment(app_image, replicas, cpu_req, mem_req, cpu_lim, mem_lim, env=None, labels=None):
    env = env or []
    labels = labels or {"app": "web"}
    return dedent(f"""
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: web
      labels: {json.dumps(labels)}
    spec:
      replicas: {replicas}
      selector:
        matchLabels: {json.dumps(labels)}
      template:
        metadata:
          labels: {json.dumps(labels)}
        spec:
          containers:
          - name: web
            image: {app_image}
            ports: [{{containerPort: 8080}}]
            resources:
              requests: {{ cpu: "{cpu_req}", memory: "{mem_req}" }}
              limits:   {{ cpu: "{cpu_lim}", memory: "{mem_lim}" }}
            readinessProbe:
              httpGet: {{ path: /healthz, port: 8080 }}
              initialDelaySeconds: 5
              periodSeconds: 5
            livenessProbe:
              httpGet: {{ path: /livez, port: 8080 }}
              initialDelaySeconds: 10
              periodSeconds: 10
            env:
    """).strip() + ("\n" if env else "\n            env: []\n") + "".join([
        f"            - name: {e['name']}\n              value: \"{e['value']}\"\n" for e in env
    ])

def tmpl_service(name, selector, port=80, target=8080):
    return dedent(f"""
    apiVersion: v1
    kind: Service
    metadata:
      name: {name}
    spec:
      selector: {json.dumps(selector)}
      ports:
      - name: http
        port: {port}
        targetPort: {target}
    """).strip()

def tmpl_ingress(host, svc, tls_secret=None):
    tls_block = f"""
      tls:
      - hosts: [ "{host}" ]
        secretName: {tls_secret}
    """ if tls_secret else ""
    return dedent(f"""
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: {svc}
    spec:{tls_block}
      rules:
      - host: {host}
        http:
          paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {svc}
                port: {{ number: 80 }}
    """).strip()

def tmpl_hpa(target, minr, maxr, cpu=70):
    return dedent(f"""
    apiVersion: autoscaling/v2
    kind: HorizontalPodAutoscaler
    metadata:
      name: {target}-hpa
    spec:
      scaleTargetRef:
        apiVersion: apps/v1
        kind: Deployment
        name: {target}
      minReplicas: {minr}
      maxReplicas: {maxr}
      metrics:
      - type: Resource
        resource:
          name: cpu
          target:
            type: Utilization
            averageUtilization: {cpu}
    """).strip()

def tmpl_network_policy(name, selector_label="app", selector_value="web"):
    return dedent(f"""
    apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      name: {name}
    spec:
      podSelector:
        matchLabels:
          {selector_label}: {selector_value}
      policyTypes: ["Ingress","Egress"]
      ingress:
      - from:
        - podSelector: {{}}
      egress:
      - to:
        - podSelector: {{}}
    """).strip()

def tmpl_redis():
    return dedent("""
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: redis }
    spec:
      replicas: 1
      selector: { matchLabels: { app: redis } }
      template:
        metadata: { labels: { app: redis } }
        spec:
          containers:
          - name: redis
            image: redis:7-alpine
            ports: [{containerPort: 6379}]
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: redis }
    spec:
      selector: { app: redis }
      ports: [{ name: redis, port: 6379, targetPort: 6379 }]
    """).strip()

def tmpl_api_deployment(image="ghcr.io/acme/api:1.0.0", replicas=2):
    return dedent(f"""
    apiVersion: apps/v1
    kind: Deployment
    metadata: {{ name: api }}
    spec:
      replicas: {replicas}
      selector: {{ matchLabels: {{ app: api }} }}
      template:
        metadata: {{ labels: {{ app: api }} }}
        spec:
          containers:
          - name: api
            image: {image}
            ports: [{{containerPort: 3000}}]
            readinessProbe:
              httpGet: {{ path: /healthz, port: 3000 }}
              initialDelaySeconds: 5
              periodSeconds: 5
            livenessProbe:
              httpGet: {{ path: /livez, port: 3000 }}
              initialDelaySeconds: 10
              periodSeconds: 10
    ---
    apiVersion: v1
    kind: Service
    metadata: {{ name: api }}
    spec:
      selector: {{ app: api }}
      ports: [{{ name: http, port: 80, targetPort: 3000 }}]
    """).strip()

def tmpl_postgres():
    return dedent("""
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata: { name: pgdata }
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 10Gi
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: postgres }
    spec:
      replicas: 1
      selector: { matchLabels: { app: postgres } }
      template:
        metadata: { labels: { app: postgres } }
        spec:
          containers:
          - name: postgres
            image: postgres:16-alpine
            env:
            - { name: POSTGRES_PASSWORD, value: example }
            ports: [{containerPort: 5432}]
            volumeMounts:
            - { name: data, mountPath: /var/lib/postgresql/data }
          volumes:
          - name: data
            persistentVolumeClaim: { claimName: pgdata }
    ---
    apiVersion: v1
    kind: Service
    metadata: { name: postgres }
    spec:
      selector: { app: postgres }
      ports: [{ name: pg, port: 5432, targetPort: 5432 }]
    """).strip()

def tmpl_rtmp_stack(host=None, tls_secret=None):
    nginx_conf = dedent("""
    worker_processes  auto;
    events { worker_connections  1024; }
    rtmp {
      server {
        listen 1935;
        chunk_size 4096;
        application live {
          live on;
          record off;
          allow publish all;
          allow play all;
        }
      }
    }
    http {
      server {
        listen 8080;
        location /live/ {
          types { application/vnd.apple.mpegurl m3u8; video/mp2t ts; }
          alias /hls/;
          add_header Cache-Control no-cache;
        }
      }
    }
    """).strip()

    cm = dedent(f"""
    apiVersion: v1
    kind: ConfigMap
    metadata: {{ name: rtmp-nginx }}
    data:
      nginx.conf: |
{chr(10).join("        " + line for line in nginx_conf.splitlines())}
    """).strip()

    deploy = dedent("""
    apiVersion: apps/v1
    kind: Deployment
    metadata: { name: rtmp }
    spec:
      replicas: 1
      selector: { matchLabels: { app: rtmp } }
      template:
        metadata: { labels: { app: rtmp } }
        spec:
          containers:
          - name: nginx-rtmp
            image: alqutami/rtmp-hls:latest
            ports:
            - { containerPort: 1935, name: rtmp }
            - { containerPort: 8080, name: http }
            volumeMounts:
            - { name: conf, mountPath: /etc/nginx/nginx.conf, subPath: nginx.conf }
            - { name: hls, mountPath: /hls }
          volumes:
          - { name: conf, configMap: { name: rtmp-nginx } }
          - name: hls
            emptyDir: {}
    """).strip()

    svc = dedent("""
    apiVersion: v1
    kind: Service
    metadata: { name: rtmp }
    spec:
      selector: { app: rtmp }
      ports:
      - { name: rtmp, port: 1935, targetPort: 1935 }
      - { name: http, port: 80, targetPort: 8080 }
    """).strip()

    ing = ""
    if host:
      ing = tmpl_ingress(host=host, svc="rtmp", tls_secret=tls_secret)

    return "\n---\n".join([cm, deploy, svc, ing].copy() if host else [cm, deploy, svc])

# ---------- Actions ----------
def apply_yaml(manifest: str, namespace: str):
    cmd = f"kubectl {context_flag} -n {namespace} apply -f -"
    p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = p.communicate(input=manifest.encode())
    return p.returncode, out.decode()

def delete_yaml(manifest: str, namespace: str):
    cmd = f"kubectl {context_flag} -n {namespace} delete -f - --ignore-not-found"
    p = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, _ = p.communicate(input=manifest.encode())
    return p.returncode, out.decode()

def get_status(kind, name, namespace):
    return sh(f"kubectl {context_flag} -n {namespace} get {kind} {name} -o wide")

def get_ingress_host(name, namespace):
    code, out = sh(f"kubectl {context_flag} -n {namespace} get ingress {name} -o json")
    if code != 0: return None
    try:
        data = json.loads(out)
        hosts = [r['host'] for r in data['spec']['rules']]
        return hosts[0] if hosts else None
    except Exception:
        return None

def get_svc_hostport(name, namespace):
    code, out = sh(f"kubectl {context_flag} -n {namespace} get svc {name} -o json")
    if code != 0: return None
    try:
        data = json.loads(out)
        port = data['spec']['ports'][0]['port']
        t = data['spec'].get('type',"ClusterIP")
        return f"{t} service on port {port}"
    except Exception:
        return None

# ---------- Tab 1: Multi-tier ----------
with tab1:
    st.subheader("Deploy Multi-tier Website")
    template = st.selectbox("Template", ["2-tier: Web + Redis", "3-tier: Web + API + Postgres"])
    colA, colB, colC = st.columns(3)
    with colA:
        replicas = st.number_input("Web replicas", min_value=1, max_value=50, value=3)
        enable_hpa = st.checkbox("Enable HPA for web", value=True)
    with colB:
        host = st.text_input("Ingress host (optional)", value="")
        tls_secret = st.text_input("TLS secret (optional)", value="")
    with colC:
        enable_np = st.checkbox("NetworkPolicy", value=False)

    st.markdown("Advanced resources")
    c1,c2,c3,c4 = st.columns(4)
    with c1:
        cpu_req = st.text_input("cpu requests", "250m")
    with c2:
        mem_req = st.text_input("mem requests", "256Mi")
    with c3:
        cpu_lim = st.text_input("cpu limits", "750m")
    with c4:
        mem_lim = st.text_input("mem limits", "512Mi")

    app_image = st.text_input("Web image", value="ghcr.io/acme/web:1.2.3")
    env_rows = st.text_area("Web env (KEY=VALUE per line)", value="REDIS_HOST=redis\nAPI_URL=http://api")
    env = []
    for line in env_rows.splitlines():
        if "=" in line:
            k,v = line.split("=",1)
            env.append({"name":k.strip(), "value":v.strip()})

    # Build manifest
    web = tmpl_web_deployment(
        app_image, replicas, cpu_req, mem_req, cpu_lim, mem_lim, env=env, labels={"app":"web"}
    )
    svc_web = tmpl_service("web", {"app":"web"})
    manifest_parts = [web, yaml_separator(), svc_web]

    if host.strip():
        manifest_parts += [yaml_separator(), tmpl_ingress(host.strip(), "web", tls_secret.strip() or None)]
    if enable_hpa:
        manifest_parts += [yaml_separator(), tmpl_hpa("web", minr=max(1, replicas//2), maxr=max(replicas, replicas*5), cpu=70)]
    if enable_np:
        manifest_parts += [yaml_separator(), tmpl_network_policy("web-netpol", "app", "web")]

    if template.startswith("2-tier"):
        manifest_parts += [yaml_separator(), tmpl_redis()]
    else:
        # 3-tier: API + Postgres + connect web env
        manifest_parts += [yaml_separator(), tmpl_api_deployment()]
        manifest_parts += [yaml_separator(), tmpl_postgres()]
        # Optional ingress for API?
        if host.strip():
            # path-based routing for demo
            ing_api = dedent(f"""
            apiVersion: networking.k8s.io/v1
            kind: Ingress
            metadata: {{ name: api }}
            spec:
              rules:
              - host: {host.strip()}
                http:
                  paths:
                  - path: /api
                    pathType: Prefix
                    backend:
                      service:
                        name: api
                        port: {{ number: 80 }}
            """).strip()
            manifest_parts += [yaml_separator(), ing_api]

    manifest = "".join(manifest_parts)

    st.code(manifest, language="yaml")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Deploy"):
        code, out = apply_yaml(manifest, namespace)
        st.text(out)
    if c2.button("Status"):
        st.text(get_status("deployments", "web", namespace)[1])
        st.text(get_status("svc", "web", namespace)[1])
        if template.startswith("2-tier"):
            st.text(get_status("deployments", "redis", namespace)[1])
        else:
            st.text(get_status("deployments", "api", namespace)[1])
            st.text(get_status("deployments", "postgres", namespace)[1])
    if c3.button("Test Endpoint"):
        if host.strip():
            st.write(f"Try: http://{host.strip()}/")
            if not template.startswith("2-tier"):
                st.write(f"API path: http://{host.strip()}/api/healthz")
        else:
            st.write(get_svc_hostport("web", namespace) or "Service info unavailable")
    if c4.button("Cleanup"):
        code, out = delete_yaml(manifest, namespace)
        st.text(out)

# ---------- Tab 2: Live streaming ----------
with tab2:
    st.subheader("Live Streaming: NGINX-RTMP + HLS")
    host_ls = st.text_input("Ingress host (for HLS/HTTP)", value="")
    tls_ls = st.text_input("TLS secret (optional)", value="")
    stream_key = st.text_input("Stream Key (auto if blank)", value="")
    if st.button("Generate Stream Key"):
        stream_key = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        st.session_state['stream_key'] = stream_key
        st.success(f"Generated: {stream_key}")

    if 'stream_key' in st.session_state and not stream_key:
        stream_key = st.session_state['stream_key']

    manifest_rtmp = tmpl_rtmp_stack(host=host_ls.strip() or None, tls_secret=tls_ls.strip() or None)
    st.code(manifest_rtmp, language="yaml")

    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Deploy RTMP/HLS"):
        code, out = apply_yaml(manifest_rtmp, namespace)
        st.text(out)
    if c2.button("Status"):
        st.text(get_status("deployments", "rtmp", namespace)[1])
        st.text(get_status("svc", "rtmp", namespace)[1])
        if host_ls.strip():
            st.text(get_status("ingress", "rtmp", namespace)[1])
    if c3.button("Cleanup RTMP/HLS"):
        code, out = delete_yaml(manifest_rtmp, namespace)
        st.text(out)

    st.markdown("FFmpeg push command (from your broadcaster):")
    rtmp_host = host_ls.strip() or "<rtmp-service-host:1935 via port-forward or LB>"
    st.code(f'ffmpeg -re -stream_loop -1 -i sample.mp4 -c:v libx264 -preset veryfast -b:v 2500k -c:a aac -f flv rtmp://{rtmp_host}/live/{stream_key or "mystream"}', language="bash")

    if host_ls.strip():
        hls_url = f"https://{host_ls.strip()}/live/{(stream_key or 'mystream')}.m3u8"
        st.markdown(f"HLS playback URL: {hls_url}")
        # Inline player via HTML
        st.components.v1.html(dedent(f"""
        <video id="video" controls autoplay style="width:100%;max-width:800px;background:#000"></video>
        <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
        <script>
          const url = "{hls_url}";
          const video = document.getElementById('video');
          if (video.canPlayType('application/vnd.apple.mpegurl')) {{
            video.src = url;
          }} else if (Hls.isSupported()) {{
            const hls = new Hls();
            hls.loadSource(url);
            hls.attachMedia(video);
          }} else {{
            document.body.insertAdjacentHTML('beforeend','<p>HLS not supported in this browser.</p>');
          }}
        </script>
        """), height=360)

# ---------- Tab 3: Cluster Status ----------
with tab3:
    st.subheader("Quick Cluster Status")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("List Pods"):
            st.text(sh(f"kubectl {context_flag} -n {namespace} get pods -o wide")[1])
        if st.button("List Services"):
            st.text(sh(f"kubectl {context_flag} -n {namespace} get svc -o wide")[1])
    with col2:
        if st.button("List Ingresses"):
            st.text(sh(f"kubectl {context_flag} -n {namespace} get ingress -o wide")[1])
        if st.button("Describe Web Deployment"):
            st.text(sh(f"kubectl {context_flag} -n {namespace} describe deploy web")[1])
