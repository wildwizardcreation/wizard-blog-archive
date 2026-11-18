# wizard-blog-archive

currently houses the ever unfinished archives for wizard's previous blogs + an active daily scraper for [the current blog](https://github.com/wildwizardcreation/wizard-blog-archive/tree/main/blogs/wonderous-wizard) 
feel free to browse and study and steal to your heart's content :] 

## post anatomy
### meta tags

<table>
  <thead>
    <tr>
      <th>name</th>
      <th>content</th>
      <th>what it means</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3">archive-source</td>
      <td>external-reblog</td>
      <td>my post, copied out of someone else's reblog (the ones that get turned back into my original).</td>
    </tr>
    <tr>
      <td>cross-blog</td>
      <td>one of my blogs reblogging a post from another of my blogs, like <code>wonderous-wizard</code> reblogging an old <code>wildwizardcreation</code> post.</td>
    </tr>
    <tr>
      <td>cross-archive</td>
      <td>an old blog's post, filed into that blog's own archive because i reblogged it on a newer blog and tagged it with one of that newer blog's <code>crossArchiveTags</code> in blogs.json (like <code>#wizards greatest hits</code>). the reblog stays in the newer blog's archive too, but is hidden from its page by default.</td>
    </tr>
    <tr>
      <td>archive-source-reblog</td>
      <td>blog-name/post-id</td>
      <td>the reblog my copy came from, like <code>staff/767793700786077696</code>. on cross-blog and cross-archive posts it's my own reblog.</td>
    </tr>
    <tr>
      <td>archive-source-blogs</td>
      <td>blog names, separated by spaces</td>
      <td>which of my other blogs show up in the reblog chain. only on cross-blog posts.</td>
    </tr>
    <tr>
      <td>archive-special-edit</td>
      <td>anything</td>
      <td>a post i've edited by hand (added tags, fixed info or links, etc). the archiver never rebuilds over these.</td>
    </tr>
  </tbody>
</table>

the same info sits on each post's `<main>` as `data-source`, `data-source-reblog-user`, `data-source-reblog-id` and `data-source-blogs`, so i can style it. posts that came straight from their original source get none of the source ones. `archive-source-reblog`'s `user/id` pair is split in two on `<main>`, since css can't split a string.

`archive-source-reblog` (and the user/id pair on `<main>`) records where my copy came from, and the raw json still saves the original data untouched.
