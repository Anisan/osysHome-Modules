# Modules - Module Management System

![Modules Icon](static/Modules.png)

A comprehensive module management system for installing, updating, and managing plugins from GitHub repositories.

## Description

The `Modules` module provides a complete module management system for the osysHome platform. It enables you to install modules from GitHub, check for updates, upgrade modules and core system, and manage module lifecycle.

## Main Features

- ✅ **Module Installation**: Install modules from GitHub repositories
- ✅ **Update Detection**: Automatic detection of module updates
- ✅ **Module Upgrade**: Upgrade modules to latest version
- ✅ **Core Upgrade**: Upgrade osysHome core system
- ✅ **Module Uninstall**: Remove modules and clean up database tables
- ✅ **Search Integration**: Search modules by name, title, or description
- ✅ **Widget Support**: Dashboard widget showing available updates
- ✅ **Cyclic Updates Check**: Background task for checking updates

## Admin Panel

The module provides a comprehensive admin interface:

### Main View
- **Modules List**: View all installed modules
- **Update Status**: See which modules have updates available
- **Module Information**: Name, title, description, version
- **Actions**: Install, upgrade, uninstall modules

### Module Operations

#### Install Module
- Install from GitHub repository
- Format: `osysHome-{module_name}`
- Automatic dependency installation from `requirements.txt`
- Database registration

#### Upgrade Module
- Upgrade to latest commit or branch
- Preserve configuration
- Automatic dependency update
- Restart notification

#### Uninstall Module
- Remove module files
- Drop database tables
- Clean up registrations
- Protected modules cannot be uninstalled

#### Core Upgrade
- Upgrade osysHome core system
- Branch selection support
- Commit-specific upgrades

## Update Detection

The module includes a cyclic task that:
- Checks GitHub for new commits
- Compares with last known update date
- Notifies about available updates
- Respects GitHub rate limits
- Supports GitHub token for higher limits

### Update Settings
- **Update Interval**: Time between update checks (default: 60 minutes)
- **GitHub Token**: Personal access token for API (optional)

## Widget

The module provides a dashboard widget showing:
- Available core updates
- Modules with available updates
- Visual indicators for updates

## Search Integration

The module provides search functionality:
- Search by module name
- Search by module title
- Search by description
- Direct links to modules

## Protected Modules

The following modules cannot be uninstalled:
- `Modules` - Module management system
- `Objects` - Object management system
- `Users` - User management
- `Scheduler` - Task scheduler
- `wsServer` - WebSocket server

## Usage

### Installing a Module

1. Navigate to Modules admin panel
2. Use install URL format: `Modules?op=install&name={module}&author={author}`
3. Module will be downloaded from GitHub
4. Dependencies installed automatically
5. Module registered in database

### Upgrading a Module

1. Navigate to Modules admin panel
2. Click upgrade on module with update available
3. Select commit (optional) or use latest
4. Module files updated
5. Dependencies updated if needed
6. System restart may be required

### Uninstalling a Module

1. Navigate to Modules admin panel
2. Click uninstall on module
3. Confirm uninstallation
4. Module files and database tables removed

## Technical Details

- **GitHub Integration**: GitHub API v3
- **Repository Format**: `osysHome-{module_name}`
- **Dependency Management**: Automatic pip install from requirements.txt
- **Database Cleanup**: Automatic table dropping on uninstall
- **Rate Limiting**: GitHub API rate limit handling
- **Threading**: Background update checking

## Version

Current version: **1.0**

## Category

System

## Actions

The module provides the following actions:
- `search` - Search modules by name, title, or description
- `cycle` - Background task for checking updates
- `widget` - Dashboard widget with update information

## Requirements

- Flask
- SQLAlchemy
- Requests
- GitHub API access
- osysHome core system

## Author

Eraser

## License

See the main osysHome project license

