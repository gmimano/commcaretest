#!/bin/sh
#
# An example hook script to verify what is about to be committed.
# Called by "git commit" with no arguments.  The hook should
# exit with non-zero status after issuing an appropriate message if
# it wants to stop the commit.
#
# To enable this hook, rename this file to "pre-commit".

if git rev-parse --verify HEAD >/dev/null 2>&1
then
	against=HEAD
else
	# Initial commit: diff against an empty tree object
	against=4b825dc642cb6eb9a060e54bf8d69288fbee4904
fi

# If you want to allow non-ascii filenames set this variable to true.
allownonascii=$(git config hooks.allownonascii)

# Redirect output to stderr.
exec 1>&2

# Cross platform projects tend to avoid non-ascii filenames; prevent
# them from being added to the repository. We exploit the fact that the
# printable range starts at the space character and ends with tilde.
if [ "$allownonascii" != "true" ] &&
	# Note that the use of brackets around a tr range is ok here, (it's
	# even required, for portability to Solaris 10's /usr/bin/tr), since
	# the square bracket bytes happen to fall in the designated range.
	test $(git diff --cached --name-only --diff-filter=A -z $against |
	  LC_ALL=C tr -d '[ -~]\0' | wc -c) != 0
then
	echo "Error: Attempt to add a non-ascii file name."
	echo
	echo "This can cause problems if you want to work"
	echo "with people on other platforms."
	echo
	echo "To be portable it is advisable to rename the file ..."
	echo
	echo "If you know what you are doing you can disable this"
	echo "check using:"
	echo
	echo "  git config hooks.allownonascii true"
	echo
	exit 1
fi

# If there are whitespace errors, print the offending file names and fail.
status=0
git diff-index --check --cached $against --
if [ $? != 0 ]; then status=1; fi
git diff --cached | pep8 --diff --show-pep8 --show-source
if [ $? != 0 ]; then status=1; fi

# connect stdin to keyboard so user can interact
exec < /dev/tty

# http://askubuntu.com/a/22257
function confirm_commit() {
    read -p 'There are formatting errors in your commit. Would you like to commit anyway? [N/y] ' -n 1 REPLY
    echo
    if test "$REPLY" = "y" -o "$REPLY" = "Y"; then
        exit 0
    else
        echo "Commit cancelled by user"
        exit 1
    fi
}

if [ $status != 0 ]
then
    confirm_commit
    if [ $? != 0 ]
    then
        exit 1
    fi
fi
