// @ts-check
// `@type` JSDoc annotations allow editor autocompletion and type checking
// (when paired with `@ts-check`).
// See: https://docusaurus.io/docs/api/docusaurus-config

import {themes as prismThemes} from 'prism-react-renderer';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'cakewalk',
  tagline: 'A SQLite-backed drop-in replacement for os.walk',
  favicon: 'img/favicon.ico',

  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  url: 'https://vxlk.github.io',
  baseUrl: '/cakewalk/',

  organizationName: 'vxlk',
  projectName: 'cakewalk',

  onBrokenLinks: 'throw',

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: './sidebars.js',
          editUrl: 'https://github.com/vxlk/cakewalk/tree/master/website/',
        },
        // No blog. This is reference documentation for a library.
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'cakewalk',
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docsSidebar',
            position: 'left',
            label: 'Docs',
          },
          {to: '/docs/api', label: 'API', position: 'left'},
          {to: '/docs/performance', label: 'Performance', position: 'left'},
          {
            href: 'https://github.com/vxlk/cakewalk',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {label: 'Getting started', to: '/docs/getting-started'},
              {label: 'Freshness', to: '/docs/freshness'},
              {label: 'Architecture', to: '/docs/architecture'},
            ],
          },
          {
            title: 'Reference',
            items: [
              {label: 'API', to: '/docs/api'},
              {label: 'Performance', to: '/docs/performance'},
              {label: 'Limitations', to: '/docs/limitations'},
            ],
          },
          {
            title: 'Project',
            items: [
              {label: 'GitHub', href: 'https://github.com/vxlk/cakewalk'},
              {label: 'Issues', href: 'https://github.com/vxlk/cakewalk/issues'},
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} cakewalk contributors. MIT licensed. Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
        additionalLanguages: ['rust', 'sql', 'bash'],
      },
    }),
};

export default config;
