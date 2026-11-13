# Documentation site

Static, dependency-free website for True HQ Audio 2.5.9.

## Local preview

Open `index.html` in a browser. For a local HTTP preview, from the project root:

```powershell
python -m http.server 8000 --directory docs
```

Then visit `http://localhost:8000`.

## GitHub Pages

`.github/workflows/pages.yml` validates local references and the application test suite, then deploys the `docs/` artifact from `main` or `master`.

In the repository settings, set **Pages → Build and deployment → Source** to **GitHub Actions**. The website URL is published in the workflow deployment environment and the Actions run summary.

The workflow supplies the real repository and Pages URL to the HTML placeholders before deployment, so the site does not depend on a hard-coded organization name.

## Content policy

The website distinguishes application intent from hardware guarantees: it does not claim to observe DAC firmware or analog output, and notes that exclusive-mode support and DSD over DoP depend on the device/driver.
