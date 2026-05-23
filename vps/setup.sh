#!/bin/bash
set -e

DOMAIN="stream.reginerd.tv"
SOURCE_PASSWORD="rgnrd_source_2024"
ADMIN_PASSWORD="rgnrd_admin_$(openssl rand -hex 8)"
SOURCE_URL="https://radio.reginerd.tv/stream"
EMAIL="reggie@reginerd.tv"

echo ">>> Installing packages..."
debconf-set-selections <<< "icecast2 icecast2/icecast-setup boolean false"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq icecast2 nginx certbot python3-certbot-nginx ffmpeg

echo ">>> Configuring Icecast2..."
cat > /etc/icecast2/icecast.xml << EOF
<icecast>
  <location>San Francisco, CA</location>
  <admin>$EMAIL</admin>
  <limits>
    <clients>100</clients>
    <sources>5</sources>
    <queue-size>524288</queue-size>
    <client-timeout>30</client-timeout>
    <header-timeout>15</header-timeout>
    <source-timeout>10</source-timeout>
    <burst-on-connect>1</burst-on-connect>
    <burst-size>65536</burst-size>
  </limits>
  <authentication>
    <source-password>$SOURCE_PASSWORD</source-password>
    <relay-password>$SOURCE_PASSWORD</relay-password>
    <admin-user>admin</admin-user>
    <admin-password>$ADMIN_PASSWORD</admin-password>
  </authentication>
  <hostname>$DOMAIN</hostname>
  <listen-socket>
    <port>8000</port>
    <bind-address>127.0.0.1</bind-address>
  </listen-socket>
  <http-headers>
    <header name="Access-Control-Allow-Origin" value="*" />
    <header name="Cache-Control" value="no-cache, no-store" />
  </http-headers>
  <mount type="normal">
    <mount-name>/stream</mount-name>
    <stream-name>REGINERD-FM</stream-name>
    <stream-description>reginerd's record collection, on the air 24/7</stream-description>
    <stream-genre>Hip-Hop / R&amp;B / VGM</stream-genre>
  </mount>
  <mount type="normal">
    <mount-name>/stream.aac</mount-name>
    <stream-name>REGINERD-FM (AAC)</stream-name>
    <stream-description>reginerd's record collection, on the air 24/7</stream-description>
    <stream-genre>Hip-Hop / R&amp;B / VGM</stream-genre>
  </mount>
  <fileserve>1</fileserve>
  <paths>
    <basedir>/usr/share/icecast2</basedir>
    <logdir>/var/log/icecast2</logdir>
    <webroot>/usr/share/icecast2/web</webroot>
    <adminroot>/usr/share/icecast2/admin</adminroot>
    <pidfile>/run/icecast2/icecast2.pid</pidfile>
  </paths>
  <logging>
    <accesslog>access.log</accesslog>
    <errorlog>error.log</errorlog>
    <loglevel>3</loglevel>
  </logging>
  <security>
    <chroot>0</chroot>
  </security>
</icecast>
EOF

echo ">>> Configuring Nginx..."
rm -f /etc/nginx/sites-enabled/default
cat > /etc/nginx/sites-available/rgnrd-stream << 'EOF'
server {
    listen 80;
    server_name stream.reginerd.tv;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }
}
EOF
ln -sf /etc/nginx/sites-available/rgnrd-stream /etc/nginx/sites-enabled/rgnrd-stream
nginx -t

echo ">>> Starting Icecast2 + Nginx..."
systemctl enable icecast2 nginx
systemctl restart icecast2 nginx

echo ">>> Getting SSL certificate (requires DNS to be set up)..."
certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m $EMAIL

echo ">>> Creating Ogg relay service..."
cat > /etc/systemd/system/rgnrd-relay.service << EOF
[Unit]
Description=RGNRD-FM Ogg Relay
After=network.target icecast2.service
Requires=icecast2.service

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStart=/usr/bin/ffmpeg \\
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 15 \\
  -i $SOURCE_URL \\
  -c:a copy -f ogg \\
  icecast://source:$SOURCE_PASSWORD@127.0.0.1:8000/stream

[Install]
WantedBy=multi-user.target
EOF

echo ">>> Creating AAC transcode service..."
cat > /etc/systemd/system/rgnrd-aac.service << EOF
[Unit]
Description=RGNRD-FM AAC Transcoder
After=network.target icecast2.service rgnrd-relay.service
Requires=icecast2.service

[Service]
Type=simple
Restart=always
RestartSec=15
ExecStartPre=/bin/sleep 8
ExecStart=/usr/bin/ffmpeg \\
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 15 \\
  -i http://127.0.0.1:8000/stream \\
  -c:a aac -b:a 128k -f adts \\
  icecast://source:$SOURCE_PASSWORD@127.0.0.1:8000/stream.aac

[Install]
WantedBy=multi-user.target
EOF

echo ">>> Starting relay services..."
systemctl daemon-reload
systemctl enable rgnrd-relay rgnrd-aac
systemctl start rgnrd-relay
sleep 10
systemctl start rgnrd-aac

echo ""
echo "✅ Done!"
echo "   Ogg:  https://$DOMAIN/stream"
echo "   AAC:  https://$DOMAIN/stream.aac"
echo "   Admin password saved: $ADMIN_PASSWORD"
echo ""
echo "Check status:"
echo "   systemctl status rgnrd-relay rgnrd-aac icecast2"
