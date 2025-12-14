# WebtoonHub

A web-based application for managing and downloading webtoons from various sources. Track your favorite series, automatically check for new episodes, and download them in your preferred format (PDF, images, ZIP, or CBZ).

## Features

- 📚 **Series Management**: Add and manage webtoon subscriptions
- 🔄 **Auto-Updates**: Automatically check for new episodes at configurable intervals
- 📥 **Download Management**: Download episodes individually or in bulk
- 🖼️ **Thumbnail Caching**: Cached episode thumbnails for faster browsing
- 📁 **Flexible Organization**: Customize download paths and file naming
- 🎨 **Modern UI**: Clean, responsive web interface
- 🐳 **Docker Support**: Easy deployment with Docker and Docker Compose

## Prerequisites

- Python 3.11+ (for local development)
- Docker and Docker Compose (for containerized deployment)
- Internet connection for scraping and downloading webtoons

## Installation

### Using Docker (Recommended)

1. Clone this repository:
   ```bash
   git clone <repository-url>
   cd webtoonhub
   ```

2. Start the application:
   ```bash
   docker-compose up -d
   ```

3. Access the web interface at `http://localhost:8128`

### Local Development

1. Clone this repository:
   ```bash
   git clone <repository-url>
   cd webtoonhub
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create necessary directories:
   ```bash
   mkdir -p downloads db cache/thumbnails
   ```

5. Run the application:
   ```bash
   python app.py
   ```

6. Access the web interface at `http://localhost:8128`

## Usage

1. **Add a Series**: Enter a webtoon series URL to subscribe to it
2. **Configure Settings**: Set your preferred download format, path template, and auto-check interval
3. **Download Episodes**: Download individual episodes or use bulk download options
4. **Monitor Progress**: Track download progress and job status in real-time

## Project Structure

```
webtoonhub/
├── app.py                 # Main Flask application
├── scrape.py              # Web scraping functionality
├── download_process.py    # Download processing logic
├── requirements.txt       # Python dependencies
├── Dockerfile             # Docker image configuration
├── docker-compose.yml     # Docker Compose configuration
├── templates/
│   └── index.html         # Web interface
├── db/                    # SQLite database (created at runtime)
├── downloads/             # Downloaded files (created at runtime)
└── cache/                 # Cached thumbnails (created at runtime)
```

## Configuration

The application uses a SQLite database to store:
- Subscriptions (series you're tracking)
- Episodes (individual episodes for each series)
- Jobs (download job history)
- Settings (global and per-series preferences)

All data is persisted in the `db/` directory when using Docker volumes.

## Development

### Running in Debug Mode

The application runs in debug mode by default when executed directly:
```bash
python app.py
```

For production deployments, ensure debug mode is disabled.

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]

## Disclaimer

This tool is for personal use only. Please respect the terms of service of webtoon platforms and copyright holders. Only download content you have the right to access.
