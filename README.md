# route179.dev

Source for [route179.dev](https://route179.dev/) — a blog on **Cloud-Native Infrastructure & AI Platform Engineering** by Sheng Chen.

Built with [Hugo](https://gohugo.io/) and the [PaperMod](https://github.com/adityatelange/hugo-PaperMod) theme, deployed to GitHub Pages via GitHub Actions. Migrated from WordPress, with original publish dates and permalinks preserved.

## Local development

```bash
# clone with the theme submodule
git clone --recurse-submodules git@github.com:sc13912/route179.dev.git
cd route179.dev

# run the live preview at http://localhost:1313
hugo server
```

If you cloned without `--recurse-submodules`, fetch the theme with:

```bash
git submodule update --init --recursive
```

## Structure

```
content/posts/          # blog posts (one page bundle per post, images alongside)
content/publications/   # external AWS publications, surfaced via the Tags taxonomy
content/aws-publications.md   # curated "AWS Publications" page
layouts/_default/term.html    # custom tag page: AWS Publications + Personal Blog
hugo.toml               # site configuration
.github/workflows/      # build + deploy to GitHub Pages
```

## Publishing

Every push to `main` triggers the GitHub Actions workflow, which builds the site
with Hugo and deploys it to GitHub Pages. No manual build step required.
