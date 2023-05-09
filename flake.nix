# A nix flake that sets up a complete Synapse development environment. Dependencies
# for the SyTest (https://github.com/matrix-org/sytest) and Complement
# (https://github.com/matrix-org/complement) Matrix homeserver test suites are also
# installed automatically.
#
# You must have already installed nix (https://nixos.org) on your system to use this.
# nix can be installed on Linux or MacOS; NixOS is not required. Windows is not
# directly supported, but nix can be installed inside of WSL2 or even Docker
# containers. Please refer to https://nixos.org/download for details.
#
# You must also enable support for flakes in Nix. See the following for how to
# do so permanently: https://nixos.wiki/wiki/Flakes#Enable_flakes
#
# Usage:
#
# With nix installed, navigate to the directory containing this flake and run
# `nix develop --impure`. The `--impure` is necessary in order to store state
# locally from "services", such as PostgreSQL and Redis.
#
# You should now be dropped into a new shell with all programs and dependencies
# availabile to you!
#
# You can start up pre-configured, local PostgreSQL and Redis instances by
# running: `devenv up`. To stop them, use Ctrl-C.
#
# A PostgreSQL database called 'synapse' will be set up for you, along with
# a PostgreSQL user named 'synapse_user'.
# The 'host' can be found by running `echo $PGHOST` with the development
# shell activated. Use these values to configure your Synapse to connect
# to the local PostgreSQL database. You do not need to specify a password.
# https://matrix-org.github.io/synapse/latest/postgres
#
# All state (the venv, postgres and redis data and config) are stored in
# .devenv/state. Deleting a file from here and then re-entering the shell
# will recreate these files from scratch.
#
# You can exit the development shell by typing `exit`, or using Ctrl-D.
#
# If you would like this development environment to activate automatically
# upon entering this directory in your terminal, first install `direnv`
# (https://direnv.net/). Then run `echo 'use flake . --impure' >> .envrc` at
# the root of the Synapse repo. Finally, run `direnv allow .` to allow the
# contents of '.envrc' to run every time you enter this directory. Voilà!

{
  inputs = {
    # Use the master/unstable branch of nixpkgs. The latest stable, 22.11,
    # does not contain 'perl536Packages.NetAsyncHTTP', needed by Sytest.
    nixpkgs.url = "github:NixOS/nixpkgs/master";
    # Output a development shell for x86_64/aarch64 Linux/Darwin (MacOS).
    systems.url = "github:nix-systems/default";
    # A development environment manager built on Nix. See https://devenv.sh.
    devenv.url = "github:cachix/devenv/main";
    # Rust toolchains and rust-analyzer nightly.
    fenix = {
      url = "github:nix-community/fenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, devenv, systems, ... } @ inputs:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
    in {
      devShells = forEachSystem (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in {
          # Everything is configured via devenv - a nix module for creating declarative
          # developer environments. See https://devenv.sh/reference/options/ for a list
          # of all possible options.
          default = devenv.lib.mkShell {
            inherit inputs pkgs;
            modules = [
              {
                # Make use of the Starship command prompt when this development environment
                # is manually activated (via `nix develop --impure`).
                # See https://starship.rs/ for details on the prompt itself.
                starship.enable = true;

                # Configure packages to install.
                # Search for package names at https://search.nixos.org/packages?channel=unstable
                packages = with pkgs; [
                  # Native dependencies for running Synapse.
                  icu
                  libffi
                  libjpeg
                  libpqxx
                  libwebp
                  libxml2
                  libxslt
                  sqlite

                  # Native dependencies for unit tests (SyTest also requires OpenSSL).
                  openssl
                  xmlsec

                  # Native dependencies for running Complement.
                  olm

                  # For building the Synapse documentation website.
                  mdbook
                ];

                # Install Python and manage a virtualenv with Poetry.
                languages.python.enable = true;
                languages.python.poetry.enable = true;
                # Automatically activate the poetry virtualenv upon entering the shell.
                languages.python.poetry.activate.enable = true;
                # Install all extra Python dependencies; this is needed to run the unit
                # tests and utilitise all Synapse features.
                languages.python.poetry.install.arguments = ["--extras all"];
                # Install the 'matrix-synapse' package from the local checkout.
                languages.python.poetry.install.installRootPackage = true;

                # This is a work-around for NixOS systems. NixOS is special in
                # that you can have multiple versions of packages installed at
                # once, including your libc linker!
                #
                # Some binaries built for Linux expect those to be in a certain
                # filepath, but that is not the case on NixOS. In that case, we
                # force compiling those binaries locally instead.
                env.POETRY_INSTALLER_NO_BINARY = "ruff";

                # Install dependencies for the additional programming languages
                # involved with Synapse development.
                #
                # * Rust is used for developing and running Synapse.
                # * Golang is needed to run the Complement test suite.
                # * Perl is needed to run the SyTest test suite.
                languages.go.enable = true;
                languages.rust.enable = true;
                languages.rust.version = "stable";
                languages.perl.enable = true;

                # Postgres is needed to run Synapse with postgres support and
                # to run certain unit tests that require postgres.
                services.postgres.enable = true;

                # On the first invocation of `devenv up`, create a database for
                # Synapse to store data in.
                services.postgres.initdbArgs = ["--locale=C" "--encoding=UTF8"];
                services.postgres.initialDatabases = [
                  { name = "synapse"; }
                ];
                # Create a postgres user called 'synapse_user' which has ownership
                # over the 'synapse' database.
                services.postgres.initialScript = ''
                CREATE USER synapse_user;
                ALTER DATABASE synapse OWNER TO synapse_user;
                '';

                # Redis is needed in order to run Synapse in worker mode.
                services.redis.enable = true;

                # Define the perl modules we require to run SyTest.
                #
                # This list was compiled by cross-referencing https://metacpan.org/
                # with the modules defined in './cpanfile' and then finding the
                # corresponding nix packages on https://search.nixos.org/packages.
                #
                # This was done until `./install-deps.pl --dryrun` produced no output.
                env.PERL5LIB = "${with pkgs.perl536Packages; makePerlPath [
                  DBI
                  ClassMethodModifiers
                  CryptEd25519
                  DataDump
                  DBDPg
                  DigestHMAC
                  DigestSHA1
                  EmailAddressXS
                  EmailMIME
                  EmailSimple  # required by Email::Mime
                  EmailMessageID  # required by Email::Mime
                  EmailMIMEContentType  # required by Email::Mime
                  TextUnidecode  # required by Email::Mime
                  ModuleRuntime  # required by Email::Mime
                  EmailMIMEEncodings  # required by Email::Mime
                  FilePath
                  FileSlurper
                  Future
                  GetoptLong
                  HTTPMessage
                  IOAsync
                  IOAsyncSSL
                  IOSocketSSL
                  NetSSLeay
                  JSON
                  ListUtilsBy
                  ScalarListUtils
                  ModulePluggable
                  NetAsyncHTTP
                  MetricsAny  # required by Net::Async::HTTP
                  NetAsyncHTTPServer
                  StructDumb
                  URI
                  YAMLLibYAML
                ]}";
              }
            ];
          };
        });
    };
}
