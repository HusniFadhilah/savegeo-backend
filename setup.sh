#!/bin/bash
# Automated Setup Script for GEE Analysis Platform
# This script automates the entire setup process

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
print_header() {
    echo -e "${BLUE}================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ $1${NC}"
}

# Check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Main setup
main() {
    print_header "GEE Analysis Platform Setup"
    echo ""
    
    # Check Python
    print_info "Checking Python installation..."
    if command_exists python3; then
        PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
        print_success "Python $PYTHON_VERSION found"
    else
        print_error "Python 3 not found. Please install Python 3.8 or higher"
        exit 1
    fi
    
    # Check pip
    if command_exists pip3; then
        print_success "pip3 found"
    else
        print_error "pip3 not found. Please install pip"
        exit 1
    fi
    
    # Create virtual environment
    print_info "Creating virtual environment..."
    if [ ! -d "venv" ]; then
        python3 -m venv venv
        print_success "Virtual environment created"
    else
        print_warning "Virtual environment already exists"
    fi
    
    # Activate virtual environment
    print_info "Activating virtual environment..."
    source venv/bin/activate
    print_success "Virtual environment activated"
    
    # Upgrade pip
    print_info "Upgrading pip..."
    pip install --upgrade pip > /dev/null 2>&1
    print_success "pip upgraded"
    
    # Install requirements
    print_info "Installing Python dependencies..."
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt > /dev/null 2>&1
        print_success "Dependencies installed"
    else
        print_error "requirements.txt not found"
        exit 1
    fi
    
    # Setup environment file
    print_info "Setting up environment configuration..."
    if [ ! -f ".env" ]; then
        if [ -f ".env.example" ]; then
            cp .env.example .env
            print_success ".env file created from .env.example"
            print_warning "Please edit .env file with your credentials"
        else
            print_error ".env.example not found"
        fi
    else
        print_warning ".env file already exists"
    fi
    
    # Check for service account key
    print_info "Checking for GEE service account key..."
    if [ -f "service-account-key.json" ]; then
        print_success "Service account key found"
        
        # Validate JSON
        if python3 -c "import json; json.load(open('service-account-key.json'))" 2>/dev/null; then
            print_success "Service account key is valid JSON"
        else
            print_error "Service account key is not valid JSON"
        fi
    else
        print_warning "service-account-key.json not found"
        print_info "Please download your GEE service account key and save it as service-account-key.json"
    fi
    
    # Create necessary directories
    print_info "Creating project directories..."
    mkdir -p logs
    mkdir -p temp
    mkdir -p exports
    mkdir -p backups
    print_success "Directories created"
    
    # Test Earth Engine initialization
    print_info "Testing Earth Engine initialization..."
    if python3 -c "import ee; ee.Initialize(); print('OK')" 2>/dev/null | grep -q "OK"; then
        print_success "Earth Engine initialized successfully"
    else
        print_warning "Earth Engine initialization failed. Please check your credentials"
    fi
    
    # Check Docker (optional)
    print_info "Checking Docker installation (optional)..."
    if command_exists docker; then
        DOCKER_VERSION=$(docker --version | cut -d' ' -f3 | tr -d ',')
        print_success "Docker $DOCKER_VERSION found"
        
        if command_exists docker-compose; then
            print_success "docker-compose found"
        else
            print_warning "docker-compose not found (optional)"
        fi
    else
        print_warning "Docker not found (optional)"
    fi
    
    # Setup complete
    echo ""
    print_header "Setup Complete!"
    echo ""
    print_info "Next steps:"
    echo "  1. Edit .env file with your credentials"
    echo "  2. Ensure service-account-key.json is in place"
    echo "  3. Run: make dev (or python app.py)"
    echo "  4. Access frontend at: http://localhost:8080"
    echo "  5. Access backend at: http://localhost:5000"
    echo ""
    print_info "Quick commands:"
    echo "  make dev          - Start development server"
    echo "  make docker-up    - Start with Docker"
    echo "  make test         - Run tests"
    echo "  make help         - Show all available commands"
    echo ""
}

# Run main function
main

# Deactivate virtual environment
deactivate 2>/dev/null || true

print_success "Setup script completed!"
echo ""
print_warning "Don't forget to activate the virtual environment before running:"
echo "  source venv/bin/activate"