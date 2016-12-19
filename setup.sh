#!/usr/bin/env bash

# Link to the folder which contains the zip archives of virtualenv
URL_VIRTUALENV=https://github.com/pypa/virtualenv/archive/

VERSION_VIRTUALENV=13.1.0

VERSION_PYTHON=$(python -c "import sys;print sys.version[:3]")

DIR_BASE=$(cd $(dirname ${BASH_SOURCE}); pwd)
DIR_ENV=${DIR_BASE}/${1:-"jenkins-env"}
DIR_TMP=${DIR_BASE}/tmp

echo "Cleaning up existent jenkins env and tmp folders"
rm -r ${DIR_ENV} ${DIR_TMP}

echo "Fetching virtualenv ${VERSION_VIRTUALENV} and creating jenkins environment"
mkdir ${DIR_TMP}
curl -L ${URL_VIRTUALENV}${VERSION_VIRTUALENV}.zip > ${DIR_TMP}/virtualenv.zip
unzip ${DIR_TMP}/virtualenv.zip -d ${DIR_TMP}
python ${DIR_TMP}/virtualenv-${VERSION_VIRTUALENV}/virtualenv.py ${DIR_ENV}

echo "Activating the new environment"
source ${DIR_ENV}/bin/activate
if [ ! -n "${VIRTUAL_ENV:+1}" ]; then
    echo "### Failure in activating the new virtual environment: '${DIR_ENV}'"
    rm -r ${DIR_ENV} ${DIR_TMP}
    exit 1
fi

echo "Installing required dependencies"
pip install --no-deps -r ${DIR_BASE}/requirements.txt

echo -e "Deactivating the environment\n"
deactivate

rm -r ${DIR_TMP}

echo -e "##################################################################\n"
echo -e "Successfully created the Jenkins environment: '${DIR_ENV}'"
echo -e "Run 'source ${DIR_ENV}/bin/activate' to activate the environment\n"

echo -e "To be able to connect to Mozilla Pulse make sure to create an"
echo -e "account at https://pulse.mozilla.org, and update .pulse_config.ini"
echo -e "with your authentication information\n"

echo -e "To submit test results to treeherder please add all necessary"
echo -e "credentials to .jenkins.properties and restart Jenkins\n"
echo -e "##################################################################\n"

if [ ! -f ".authentication.ini" ]; then
  cp config/authentication.ini ./.authentication.ini
fi

if [ ! -f ".jenkins.properties" ]; then
  cp config/jenkins.properties ./.jenkins.properties
fi
