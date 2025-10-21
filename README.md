# GEE Land Cover & Vegetation Analysis Platform

Full-stack application for analyzing land cover and vegetation indices using Google Earth Engine.

## 📋 Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [API Documentation](#api-documentation)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)

## ✨ Features

### Backend (Flask + GEE)
- ✅ Google Earth Engine integration
- ✅ Sentinel-2 imagery processing
- ✅ 8 Vegetation indices (NDVI, NDWI, MNDWI, NDBI, EVI, SAVI, BSI, NDMI)
- ✅ Land cover analysis (Dynamic World, ESA WorldCover)
- ✅ Time series analysis
- ✅ Indonesia administrative boundaries API
- ✅ RESTful API with CORS support
- ✅ Statistical calculations
- ✅ Tile layer generation

### Frontend (HTML + Bootstrap + jQuery)
- ✅ Interactive map interface (Leaflet.js)
- ✅ Multiple AOI selection methods
- ✅ Real-time analysis visualization
- ✅ Statistical charts (Chart.js)
- ✅ Export functionality
- ✅ Responsive design

## 🏗️ Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│                 │         │                  │         │                 │
│  Frontend       │────────▶│  Flask Backend   │────────▶│  Google Earth   │
│  (HTML/jQuery)  │  AJAX   │  (Python API)    │   API   │  Engine         │
│                 │◀────────│                  │◀────────│                 │
└─────────────────┘  JSON   └──────────────────┘  Data   └─────────────────┘
                                     │
                                     ▼
                            ┌──────────────────┐
                            │  Indonesia Admin │
                            │  Boundaries API  │
                            └──────────────────┘
```

## 📦 Prerequisites

### System Requirements
- Python 3.8 or higher
- Node.js (optional, for frontend development)
- Docker & Docker Compose (optional, for containerized deployment)

### Google Earth Engine Setup

1. **Create GEE Account**
   - Visit [Google Earth Engine](https://earthengine.google.com/)
   - Sign up for an account

2. **Create Service Account**
   ```bash
   # Go to Google Cloud Console
   # Navigate to: IAM & Admin > Service Accounts
   # Create a new service account with Earth Engine permissions
   ```

3. **Generate Key File**
   ```bash
   # Download the JSON key file
   # Save as: service-account-key.json
   ```

4. **Register Service Account with GEE**
   ```bash
   # In Cloud Console, note your service account email
   # Register it at: https://signup.earthengine.google.com/
   ```

## 🚀 Installation

### Option 1: Manual Setup

#### 1. Clone Repository
```bash
git clone https://github.com/yourusername/gee-analysis.git
cd gee-analysis
```

#### 2. Create Virtual Environment
```bash
python -m venv venv

# Activate (Linux/Mac)
source venv/bin/activate

# Activate (Windows)
venv\Scripts\activate
```

#### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

#### 4. Setup Environment Variables
```bash
cp .env.example .env
# Edit .env with your credentials
```

#### 5. Place Service Account Key
```bash
# Copy your service-account-key.json to project root
cp /path/to/your/key.json ./service-account-key.json
```

### Option 2: Docker Setup

#### 1. Clone Repository
```bash
git clone https://github.com/yourusername/gee-analysis.git
cd gee-analysis
```

#### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

#### 3. Place Service Account Key
```bash
cp /path/to/your/key.json ./service-account-key.json
```

#### 4. Build and Run
```bash
docker-compose up -d
```

## ⚙️ Configuration

### Environment Variables (.env)

```bash
# Google Earth Engine
GEE_SERVICE_ACCOUNT=your-sa@your-project.iam.gserviceaccount.com
GEE_KEY_FILE=service-account-key.json

# Flask
FLASK_ENV=development
DEBUG=True
PORT=5000

# CORS (comma-separated origins)
ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8080
```

### Frontend Configuration

Update the API endpoint in your HTML file:

```javascript
// In the HTML file, update the API_BASE_URL
const API_BASE_URL = 'http://localhost:5000/api';
```

## 🏃 Running the Application

### Development Mode

#### Start Backend
```bash
# Activate virtual environment
source venv/bin/activate

# Run Flask development server
python app.py
```

Backend will run on: `http://localhost:5000`

#### Start Frontend
```bash
# Option 1: Python HTTP Server
cd frontend
python -m http.server 8080

# Option 2: Use Live Server extension in VS Code
# Right-click index.html > Open with Live Server
```

Frontend will run on: `http://localhost:8080`

### Production Mode

#### Using Gunicorn
```bash
gunicorn --bind 0.0.0.0:5000 --workers 4 --timeout 120 app:app
```

#### Using Docker
```bash
docker-compose up -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

## 📚 API Documentation

### Base URL
```
http://localhost:5000/api
```

### Endpoints

#### 1. Health Check
```http
GET /api/health
```

**Response:**
```json
{
  "status": "ok",
  "ee_initialized": true,
  "timestamp": "2025-10-21T10:00:00"
}
```

#### 2. Get Provinces
```http
GET /api/regions/provinces
```

**Response:**
```json
{
  "JAWA TENGAH": "33",
  "JAWA TIMUR": "35",
  ...
}
```

#### 3. Get Cities
```http
GET /api/regions/cities?province_code=33
```

**Response:**
```json
{
  "KOTA SEMARANG": "3374",
  "KOTA SURAKARTA": "3372",
  ...
}
```

#### 4. Get Region Geometry
```http
GET /api/regions/geometry?endpoint=city&code=3374
```

**Response:**
```json
{
  "type": "FeatureCollection",
  "features": [...]
}
```

#### 5. Analyze Vegetation
```http
POST /api/analyze/vegetation
Content-Type: application/json

{
  "aoi": {
    "west": 110.3,
    "south": -7.05,
    "east": 110.5,
    "north": -6.9
  },
  "year": 2022,
  "start_month": 6,
  "end_month": 9,
  "cloud_threshold": 40,
  "indices": ["NDVI", "NDWI"]
}
```

**Response:**
```json
{
  "collection_size": 45,
  "date_range": {
    "start": "2022-06-01",
    "end": "2022-10-01"
  },
  "rgb_tile_url": "https://earthengine.googleapis.com/...",
  "indices": {
    "NDVI": {
      "min": -0.1234,
      "mean": 0.4567,
      "max": 0.8901,
      "std_dev": 0.1234,
      "description": "General vegetation health",
      "tile_url": "https://earthengine.googleapis.com/..."
    }
  }
}
```

#### 6. Analyze Land Cover
```http
POST /api/analyze/landcover
Content-Type: application/json

{
  "aoi": {
    "west": 110.3,
    "south": -7.05,
    "east": 110.5,
    "north": -6.9
  },
  "year": 2022,
  "datasets": ["Dynamic_World"],
  "dw_mode": "mode"
}
```

**Response:**
```json
{
  "Dynamic_World": {
    "classes": {
      "Trees": {
        "area": 1250.5,
        "percentage": 35.2,
        "color": "#397D49"
      },
      "Grass": {
        "area": 890.3,
        "percentage": 25.1,
        "color": "#88B053"
      }
    },
    "tile_url": "https://earthengine.googleapis.com/..."
  }
}
```

#### 7. Time Series Analysis
```http
POST /api/timeseries
Content-Type: application/json

{
  "aoi": {
    "west": 110.3,
    "south": -7.05,
    "east": 110.5,
    "north": -6.9
  },
  "year": 2022,
  "index": "NDVI",
  "interval": "monthly"
}
```

**Response:**
```json
{
  "index": "NDVI",
  "year": 2022,
  "interval": "monthly",
  "data": [
    {
      "period": "2022-01",
      "value": 0.456
    },
    ...
  ]
}
```

## 🚢 Deployment

### Deploy to Heroku

#### 1. Install Heroku CLI
```bash
# macOS
brew install heroku/brew/heroku

# Ubuntu
curl https://cli-assets.heroku.com/install.sh | sh
```

#### 2. Login and Create App
```bash
heroku login
heroku create gee-analysis-app
```

#### 3. Set Environment Variables
```bash
heroku config:set GEE_SERVICE_ACCOUNT=your-sa@project.iam.gserviceaccount.com
heroku config:set GEE_KEY_FILE=service-account-key.json

# Add service account key as config var
heroku config:set GEE_KEY="$(cat service-account-key.json)"
```

#### 4. Create Procfile
```bash
echo "web: gunicorn --bind 0.0.0.0:$PORT --workers 4 --timeout 120 app:app" > Procfile
```

#### 5. Deploy
```bash
git add .
git commit -m "Initial deployment"
git push heroku main
```

### Deploy to Google Cloud Run

#### 1. Build Container
```bash
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/gee-backend
```

#### 2. Deploy
```bash
gcloud run deploy gee-backend \
  --image gcr.io/YOUR_PROJECT_ID/gee-backend \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars GEE_SERVICE_ACCOUNT=your-sa@project.iam.gserviceaccount.com
```

### Deploy to AWS EC2

#### 1. Launch EC2 Instance
```bash
# Ubuntu 22.04 LTS
# t2.medium or larger
# Open ports: 80, 443, 5000
```

#### 2. SSH and Install Dependencies
```bash
ssh -i your-key.pem ubuntu@your-ec2-ip

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python
sudo apt install python3-pip python3-venv -y

# Install Nginx
sudo apt install nginx -y
```

#### 3. Clone and Setup
```bash
git clone https://github.com/yourusername/gee-analysis.git
cd gee-analysis

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 4. Configure Nginx
```bash
sudo nano /etc/nginx/sites-available/gee-backend

# Add configuration
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

# Enable site
sudo ln -s /etc/nginx/sites-available/gee-backend /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

#### 5. Create Systemd Service
```bash
sudo nano /etc/systemd/system/gee-backend.service

# Add content
[Unit]
Description=GEE Backend API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/gee-analysis
Environment="PATH=/home/ubuntu/gee-analysis/venv/bin"
ExecStart=/home/ubuntu/gee-analysis/venv/bin/gunicorn --bind 127.0.0.1:5000 --workers 4 app:app

[Install]
WantedBy=multi-user.target

# Start service
sudo systemctl daemon-reload
sudo systemctl start gee-backend
sudo systemctl enable gee-backend
```

## 🔧 Troubleshooting

### Issue: Earth Engine not initialized

**Solution:**
```bash
# Check service account key file exists
ls -la service-account-key.json

# Verify service account email
cat service-account-key.json | grep client_email

# Test Earth Engine authentication
python -c "import ee; ee.Initialize(); print('Success!')"
```

### Issue: CORS errors in frontend

**Solution:**
```python
# In app.py, update CORS configuration
CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:8080", "your-frontend-domain.com"]
    }
})
```

### Issue: Timeout errors on large AOI

**Solution:**
```python
# Increase timeout in gunicorn
gunicorn --timeout 300 app:app

# Or in code, use bestEffort=True
.reduceRegion(..., bestEffort=True)
```

### Issue: Memory errors

**Solution:**
```python
# Reduce maxPixels
.reduceRegion(..., maxPixels=1e7)

# Increase scale (lower resolution)
.reduceRegion(..., scale=100)
```

### Issue: No images found

**Solution:**
- Increase cloud threshold
- Expand date range
- Check AOI coordinates are valid
- Verify Sentinel-2 coverage for your area

## 📝 Development Tips

### Testing API Endpoints

```bash
# Install HTTPie
pip install httpie

# Test health check
http GET localhost:5000/api/health

# Test vegetation analysis
http POST localhost:5000/api/analyze/vegetation \
  aoi:='{"west":110.3,"south":-7.05,"east":110.5,"north":-6.9}' \
  year:=2022 \
  indices:='["NDVI","NDWI"]'
```

### Frontend Development

```javascript
// Enable debug mode
localStorage.setItem('debug', 'true');

// View API responses in console
$(document).ajaxSuccess(function(event, xhr, settings) {
    console.log('API Call:', settings.url);
    console.log('Response:', xhr.responseJSON);
});
```

### Performance Optimization

```python
# Use memcached for caching
from werkzeug.contrib.cache import MemcachedCache
cache = MemcachedCache(['127.0.0.1:11211'])

# Cache expensive operations
@cache.memoize(timeout=3600)
def get_statistics(aoi, year):
    # ... expensive operation
    pass
```

## 📄 License

MIT License - see LICENSE file for details

## 🤝 Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit changes (`git commit -m 'Add AmazingFeature'`)
4. Push to branch (`git push origin feature/AmazingFeature`)
5. Open Pull Request

## 📧 Support

- **Issues:** https://github.com/yourusername/gee-analysis/issues
- **Email:** support@yoursite.com
- **Documentation:** https://docs.yoursite.com

## 🙏 Acknowledgments

- Google Earth Engine Team
- Bootstrap Team
- Leaflet.js Contributors
- Flask Community
- Indonesia SP3STAB API

---

**Built with ❤️ using Google Earth Engine**